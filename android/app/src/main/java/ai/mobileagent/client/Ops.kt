package ai.mobileagent.client

import android.app.KeyguardManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.PowerManager
import android.util.Base64
import org.json.JSONArray
import org.json.JSONObject

/** Executes one request from the server (the protocol in docs/phone-client.md) and returns its JSON result. */
object Ops {
    private fun a11y(): AgentAccessibilityService = AgentAccessibilityService.instance
        ?: throw IllegalStateException("the accessibility service is off: turn on Mobile Agent in Settings > Accessibility")

    fun size(c: Context): IntArray = AgentAccessibilityService.instance?.screenSize()
        ?: c.getSystemService(android.view.WindowManager::class.java).maximumWindowMetrics.bounds.let { intArrayOf(it.width(), it.height()) }

    @Suppress("DEPRECATION")
    private fun wakeScreen(c: Context) {
        val pm = c.getSystemService(PowerManager::class.java)
        if (pm.isInteractive) return
        pm.newWakeLock(PowerManager.FULL_WAKE_LOCK or PowerManager.ACQUIRE_CAUSES_WAKEUP, "mobileagent:wake").acquire(8000)
        Thread.sleep(600)
    }

    /** Power and lock state, read without waking anything: {"interactive","locked","secure","hour"} (hour is the phone's local hour 0-23). */
    private fun screenState(c: Context): JSONObject {
        val km = c.getSystemService(KeyguardManager::class.java)
        return JSONObject().put("interactive", c.getSystemService(PowerManager::class.java).isInteractive)
            .put("locked", km.isKeyguardLocked).put("secure", km.isDeviceSecure)
            .put("hour", java.util.Calendar.getInstance().get(java.util.Calendar.HOUR_OF_DAY))
    }

    private fun launch(c: Context, intent: Intent) {
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        c.startActivity(intent)
    }

    fun handle(c: Context, op: String, args: JSONObject): Any? {
        when (op) {
            // verification codes (OtpRelay): they must not wake the screen or need the accessibility service to answer
            "otp_watch" -> return OtpRelay.watch(c, args.optInt("seconds", 60), args.optLong("since_ms", 0L))
            "otp_poll" -> return OtpRelay.poll(c)
            "otp_ack" -> return OtpRelay.ack(args.optString("id"))
            "otp_stop" -> return OtpRelay.stop(c)
            // the screen and the notifications: answered without the accessibility service (screen_state, wake) or from what it saw (notifications_recent)
            "screen_state" -> return screenState(c)
            "wake" -> {  // wakes the screen and NEVER unlocks it: no PIN, no pattern, no dismissing the keyguard
                wakeScreen(c)
                if (!c.getSystemService(PowerManager::class.java).isInteractive) {  // some makers (MIUI) ignore the wake lock: a see-through activity that only asks for the screen to turn on
                    runCatching { launch(c, Intent(c, WakeActivity::class.java)) }
                }
                Thread.sleep(100)
                return screenState(c)
            }
            "notifications_recent" -> return NotifFeed.recent(c, args.optLong("since_ms", 0L), args.optInt("limit", 20))
            "size" -> return JSONArray(size(c).toList())
            "packages" -> return JSONArray(c.packageManager.getInstalledApplications(0).map { it.packageName }.sorted())
            "launcher_apps" -> {
                val q = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
                return JSONArray(c.packageManager.queryIntentActivities(q, 0).map { it.activityInfo.packageName }.distinct().sorted())
            }
            "crashes" -> return JSONArray()
            "open_app" -> {
                wakeScreen(c)
                val pkg = args.getString("package")
                launch(c, c.packageManager.getLaunchIntentForPackage(pkg) ?: throw IllegalArgumentException("$pkg is not installed or has no launcher icon"))
                return null
            }
            "open_url" -> { wakeScreen(c); launch(c, Intent(Intent.ACTION_VIEW, Uri.parse(args.getString("url")))); return null }
            "open_intent" -> { wakeScreen(c); launch(c, Intent(args.getString("action"))); return null }
        }
        wakeScreen(c)
        val s = a11y()
        return when (op) {
            "screenshot" -> Base64.encodeToString(s.screenshotPng(), Base64.NO_WRAP)
            "elements" -> s.elements(args.optInt("limit", 70))
            "tap" -> { s.tap(args.getInt("x"), args.getInt("y")); null }
            "swipe" -> { s.swipe(args.getInt("x1"), args.getInt("y1"), args.getInt("x2"), args.getInt("y2"), args.optLong("ms", 300)); null }
            "type" -> { s.typeText(args.getString("text")); null }
            "key" -> { s.key(args.getString("name")); null }
            "foreground" -> s.foregroundPackage()
            else -> throw IllegalArgumentException("unknown op '$op'")
        }
    }
}
