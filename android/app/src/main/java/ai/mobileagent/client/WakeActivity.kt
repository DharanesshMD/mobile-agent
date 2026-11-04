package ai.mobileagent.client

import android.app.Activity
import android.os.Bundle
import android.os.Handler
import android.os.Looper

/**
 * A see-through activity for the `wake` op on phones where the wake lock alone does not turn the screen on. It asks for the screen to turn on and
 * to show above the lock screen, then closes itself. It never asks to dismiss the keyguard: the phone stays locked.
 */
class WakeActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setTurnScreenOn(true)
        setShowWhenLocked(true)
        Handler(Looper.getMainLooper()).postDelayed({ finish() }, 400)
    }
}
