package ai.mobileagent.client

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Handler
import android.os.Looper
import org.json.JSONArray
import org.json.JSONObject
import java.security.SecureRandom

/**
 * Lets a task on a cloud phone fetch a one-time code that was sent to THIS phone (docs/phone-client.md, "Verification codes").
 *
 * The server opens a watch window (`otp_watch`, at most 180 s). Only while it is open, the accessibility service passes notification events here
 * and a code found in their text is kept in memory (`otp_poll` returns it, `otp_ack` forgets it). A visible notification tells the owner it is watching.
 * Nothing is stored, logged or shown: no digits in Ops logs, Logcat, preferences, files or notifications. The owner's switch (Prefs flag "otp_relay") is off by default.
 */
object OtpRelay {
    const val FLAG = "otp_relay"
    private const val MIN_S = 10
    private const val MAX_S = 180
    private const val RING = 5
    private const val KEEP_MS = 10 * 60 * 1000L
    private const val CHANNEL = "otp_watch"
    private const val NOTIFICATION_ID = 8  // AgentService holds 7 for its foreground notification
    const val ACTION_STOP = "ai.mobileagent.client.OTP_STOP"

    private class Candidate(val id: String, val code: String, val source: String, val title: String, val atMs: Long)

    private val lock = Any()
    private val found = ArrayList<Candidate>()
    private var watchStart = 0L
    private var watchEnd = 0L
    private var sinceMs = 0L
    private var redacted = false
    private var baseline = emptySet<String>()  // rows already on the lock screen or shade when the window opened: their codes are old, never offered
    private val rnd = SecureRandom()
    private val ui = Handler(Looper.getMainLooper())
    private var expiry: Runnable? = null
    private var appContext: Context? = null

    fun enabledByOwner(c: Context) = Prefs.flag(c, FLAG)

    /** Counters only (no text, no digits) for the debug receiver: how many notification events arrived, were skipped, parsed, and yielded a code. */
    @Volatile var seen = 0; @Volatile var gated = 0; @Volatile var unparsed = 0; @Volatile var parsed = 0; @Volatile var coded = 0
    fun diag() = "events=$seen gated=$gated unparsed=$unparsed parsed=$parsed coded=$coded watching=${watching()}"

    private fun now() = System.currentTimeMillis()

    fun watching(): Boolean = synchronized(lock) { now() < watchEnd }

    // ---- the ops ------------------------------------------------------------------------------------------------------------------------------

    fun watch(c: Context, seconds: Int, since: Long): JSONObject {
        val ctx = c.applicationContext
        val reason = when {
            !enabledByOwner(ctx) -> "off"
            AgentAccessibilityService.instance == null -> "no_accessibility"
            !ctx.getSystemService(NotificationManager::class.java).areNotificationsEnabled() -> "notifications_blocked"  // no visible notice, no watching
            else -> ""
        }
        if (reason.isNotEmpty()) return JSONObject().put("enabled", false).put("watching", false).put("reason", reason)
        val s = seconds.coerceIn(MIN_S, MAX_S)
        synchronized(lock) {
            appContext = ctx
            val t = now()
            if (t >= watchEnd) {  // a new window starts clean
                watchStart = t; found.clear(); redacted = false
                baseline = AgentAccessibilityService.instance?.visibleNotifications().orEmpty().map { it[1] + "\u0000" + it[2] }.toSet()
            }
            sinceMs = if (since > 0) since else watchStart
            val wanted = t + s * 1000L
            watchEnd = if (t < watchEnd) maxOf(watchEnd, wanted) else wanted  // asking again extends the window, never shortens it (and never past 180 s from now)
            expiry?.let { ui.removeCallbacks(it) }
            expiry = Runnable { stop(ctx) }.also { ui.postDelayed(it, watchEnd - t) }
        }
        showNotice(ctx, s)
        OpsLog.add(ctx, "otp_watch $s s", "")
        return JSONObject().put("enabled", true).put("watching", true).put("reason", "")
    }

    /** While the window is open and the lock screen or shade is showing, a code in a visible notification row counts too (the events carry no text under a secure lock). */
    private fun scanVisible() {
        val rows = AgentAccessibilityService.instance?.visibleNotifications().orEmpty()
        if (rows.isEmpty()) return
        synchronized(lock) {
            val t = now()
            if (t >= watchEnd) return
            for (r in rows) {
                if ((r[1] + "\u0000" + r[2]) in baseline) continue
                val code = OtpExtract.extract(r[1] + "\n" + r[2], trusted = true) ?: continue
                val source = r[0].take(40).ifEmpty { "Notification" }
                if (found.any { it.code == code && t - it.atMs < 120_000 }) continue
                val idBytes = ByteArray(4).also { rnd.nextBytes(it) }
                found.add(Candidate(idBytes.joinToString("") { "%02x".format(it) }, code, source, r[1].take(40), t))
                while (found.size > RING) found.removeAt(0)
            }
        }
    }

    fun poll(c: Context): JSONObject {
        scanVisible()
        val t = now()
        val out = JSONObject()
        synchronized(lock) {
            found.removeAll { t - it.atMs > KEEP_MS }
            val open = t < watchEnd
            val arr = JSONArray()
            for (cand in found.sortedByDescending { it.atMs }.take(RING)) {
                arr.put(JSONObject().put("id", cand.id).put("code", cand.code).put("source", cand.source).put("title", cand.title).put("at_ms", cand.atMs))
            }
            out.put("watching", open).put("remaining_s", if (open) ((watchEnd - t + 999) / 1000).toInt() else 0).put("codes", arr).put("redacted", redacted && found.isEmpty())
            if (arr.length() > 0) OpsLog.add(c.applicationContext, "otp_poll: ${arr.length()} candidate${if (arr.length() == 1) "" else "s"}", "")
        }
        return out
    }

    fun ack(id: String): JSONObject {
        synchronized(lock) { found.removeAll { it.id == id } }
        return JSONObject().put("ok", true)
    }

    /** Closes the window and forgets every code. Called by otp_stop, the Stop button, the timer, and when the service or the connection ends. */
    fun stop(c: Context? = null): JSONObject {
        val ctx = (c ?: appContext)?.applicationContext
        synchronized(lock) {
            watchEnd = 0L
            found.clear()
            redacted = false
            expiry?.let { ui.removeCallbacks(it) }
            expiry = null
        }
        if (ctx != null) runCatching { ctx.getSystemService(NotificationManager::class.java).cancel(NOTIFICATION_ID) }
        return JSONObject().put("ok", true)
    }

    // ---- the capture --------------------------------------------------------------------------------------------------------------------------

    /** A parsed notification from the accessibility service; does nothing unless a watch window is open. */
    fun consume(c: Context, p: NotifParse.Parsed) {
        val t = p.atMs
        synchronized(lock) { if (t >= watchEnd || t < sinceMs) return }
        val code = OtpExtract.extract(p.body, trusted = p.messageLike)
        if (code != null) coded++
        if (code == null) {
            // a message that arrived with no digits at all, or with the system's placeholder, was probably hidden from us (lock screen, Android 15)
            if (p.hidden || (p.messageLike && p.body.none { it.isDigit() })) synchronized(lock) { redacted = true }  // content we could not read may have been the message
            return
        }
        synchronized(lock) {
            if (found.any { it.code == code && it.source == p.label && t - it.atMs < 60_000 }) return  // the same notification updated itself
            val idBytes = ByteArray(4).also { rnd.nextBytes(it) }
            found.add(Candidate(idBytes.joinToString("") { "%02x".format(it) }, code, p.label.take(40), p.title.take(40), t))
            while (found.size > RING) found.removeAt(0)
        }
    }

    // ---- the visible notice ------------------------------------------------------------------------------------------------------------------

    private fun showNotice(c: Context, seconds: Int) {
        val nm = c.getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CHANNEL, "Verification code watch", NotificationManager.IMPORTANCE_LOW))
        val stop = PendingIntent.getBroadcast(c, 2, Intent(c, StopReceiver::class.java).setAction(ACTION_STOP), PendingIntent.FLAG_IMMUTABLE)
        val minutes = (seconds + 59) / 60
        val n = Notification.Builder(c, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_menu_view)
            .setContentTitle("Mobile Agent")
            .setContentText("Watching for a verification code for your assistant (${if (minutes <= 1) "1 min" else "$minutes min"})")  // never the code itself
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .addAction(Notification.Action.Builder(null, "Stop", stop).build())
            .build()
        nm.notify(NOTIFICATION_ID, n)
    }

    /** The Stop button of the "watching" notice. */
    class StopReceiver : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) { stop(context) }
    }
}
