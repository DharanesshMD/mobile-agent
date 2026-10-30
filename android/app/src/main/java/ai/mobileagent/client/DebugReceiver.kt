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
