package ai.mobileagent.client

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.ApplicationInfo
import android.util.Log

/**
 * Test hook (debuggable builds only): pair and start without typing.
 *   adb shell am broadcast -n ai.mobileagent.client/.DebugReceiver --es server http://10.0.2.2:8001 --es code ABCD-EFGH
 *   adb shell am start -n ai.mobileagent.client/.MainActivity      (starts the service for a paired phone)
 *   adb shell am broadcast -n ai.mobileagent.client/.DebugReceiver --es action stop
 */
class DebugReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (context.applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE == 0) return
        if (intent.getStringExtra("action") == "stop") {
            AgentService.stop(context)
            return
        }
        if (intent.getStringExtra("action") == "a11y_ids") {  // the structure of the screen in front (view ids, classes, depth; no texts) to adapt the notification reader
            val root = AgentAccessibilityService.instance?.rootInActiveWindow
            fun walk(n: android.view.accessibility.AccessibilityNodeInfo, d: Int) {
                if (d > 14) return
                if (!n.text.isNullOrBlank()) Log.i("MobileAgent", "node d=$d id=${n.viewIdResourceName} cls=${n.className?.toString()?.substringAfterLast('.')} len=${n.text.length} pkg=${n.packageName}")
                for (i in 0 until n.childCount) n.getChild(i)?.let { walk(it, d + 1) }
            }
            if (root != null) walk(root, 0) else Log.i("MobileAgent", "node none")
            return
        }
        if (intent.getStringExtra("action") == "wake_activity") {  // starts the screen-on trampoline from this app's own process, as the wake op does (adb may not start a non-exported activity)
            context.startActivity(Intent(context, WakeActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            return
        }
        if (intent.getStringExtra("action") == "otp_diag") {  // counters only: never a text or a digit
            Log.i("MobileAgent", "otp diag " + OtpRelay.diag())
            return
        }
        if (intent.getStringExtra("action") == "otp_selftest") {  // runs the code finder on a fixed table; logs only pass or the failing case numbers
            Log.i("MobileAgent", "otp extractor " + OtpExtract.selfTest())
            return
        }
        val server = intent.getStringExtra("server") ?: Prefs.server(context)
        val result = goAsync()
        Thread {
            try {
                val token = intent.getStringExtra("token") ?: Pairing.pair(server, intent.getStringExtra("code").orEmpty())
                Prefs.savePairing(context, server, token)
                Prefs.setEnabled(context, true)  // the screen starts the service the next time it is opened (a background receiver may not)
                Log.i("MobileAgent", "paired")
            } catch (e: Exception) {
                Log.w("MobileAgent", "debug pairing failed: ${e.message}")
            } finally {
                result.finish()
            }
        }.start()
    }
}
