package ai.mobileagent.client

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject

/**
 * "What did I miss": the notifications that arrived while the accessibility service ran, for the op `notifications_recent` (docs/phone-client.md).
 * Opt-in (Prefs flag "notif_read", default off). Memory only: at most 50 entries and 24 hours, wiped when the service stops, when the owner switches it off
 * and when the app's data is cleared (nothing is ever written to disk). Verification codes are replaced by "[code]" before a text is kept. Texts are never logged.
 */
object NotifFeed {
    const val FLAG = "notif_read"
    private const val MAX = 50
    private const val KEEP_MS = 24 * 60 * 60 * 1000L

    private class Item(val key: String, val app: String, val pkg: String, val title: String, val text: String, val atMs: Long, val hidden: Boolean)

    private val lock = Any()
    private val items = ArrayList<Item>()

    fun enabled(c: Context) = Prefs.flag(c, FLAG)

    /** Called by the accessibility service for each notification event; does nothing unless the owner turned this on. */
    fun add(c: Context, p: NotifParse.Parsed) {
        if (!enabled(c) || p.ongoing) return
        // A group summary or a system stand-in with no content at all is noise while the phone is unlocked; under a lock it tells the assistant the phone is hiding details.
        if (p.body.isEmpty() && !c.getSystemService(android.app.KeyguardManager::class.java).isKeyguardLocked) return
        val title = OtpExtract.maskCodes(p.title, p.messageLike).take(80)
        val text = if (p.hidden) "" else OtpExtract.maskCodes(p.text, p.messageLike).take(300)
        synchronized(lock) {
            val t = System.currentTimeMillis()
            // an update of the same notification replaces its older entry (same app, title and post time, or the very same words within 5 s)
            items.removeAll { t - it.atMs > KEEP_MS || it.key == p.key || (it.pkg == p.pkg && it.title == title && it.text == text && p.atMs - it.atMs < 5000) }
            items.add(Item(p.key, p.label.take(40), p.pkg, title, text, p.atMs, p.hidden))
            while (items.size > MAX) items.removeAt(0)
        }
    }

    fun recent(c: Context, since: Long, limit: Int): JSONObject {
        val reason = when {
            !enabled(c) -> "off"
            AgentAccessibilityService.instance == null -> "no_accessibility"
            else -> ""
        }
        if (reason.isEmpty()) addVisible(c)  // what the lock screen or shade shows right now (events carry no text while a secure lock is on)
        val arr = JSONArray()
        if (reason.isEmpty()) synchronized(lock) {
            val t = System.currentTimeMillis()
            items.removeAll { t - it.atMs > KEEP_MS }
            for (i in items.filter { it.atMs >= since }.sortedByDescending { it.atMs }.take(limit.coerceIn(1, MAX))) {
                arr.put(JSONObject().put("app", i.app).put("pkg", i.pkg).put("title", i.title).put("text", i.text).put("at_ms", i.atMs).put("hidden", i.hidden))
            }
        }
        if (reason.isEmpty()) OpsLog.add(c.applicationContext, "notifications_recent: ${arr.length()} item${if (arr.length() == 1) "" else "s"}", "")
        return JSONObject().put("enabled", reason.isEmpty()).put("reason", reason).put("items", arr)
    }

    /** Rows read from the screen are added once: an identical title and text already in memory is not stored again. Their time is when they were read. */
    private fun addVisible(c: Context) {
        val rows = AgentAccessibilityService.instance?.visibleNotifications().orEmpty()
        if (rows.isEmpty()) return
        synchronized(lock) {
            val t = System.currentTimeMillis()
            for (r in rows) {
                val title = OtpExtract.maskCodes(r[1], true).take(80)
                val text = OtpExtract.maskCodes(r[2], true).take(300)
                if (items.any { it.title == title && it.text == text }) continue
                items.removeAll { it.hidden && (it.app == r[0].take(40) || it.title == it.app) && t - it.atMs < 60_000 }  // a stand-in for this very notification
                items.add(Item("screen|$title|${text.hashCode()}", r[0].take(40).ifEmpty { "Notification" }, "", title, text, t, false))
            }
            while (items.size > MAX) items.removeAt(0)
        }
    }

    fun clear() { synchronized(lock) { items.clear() } }
}
