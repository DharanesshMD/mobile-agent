package ai.mobileagent.client

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.text.InputType
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast

/** One screen: pair with the server, turn the accessibility service on, allow background running, start or stop. */
class MainActivity : Activity() {
    private lateinit var status: TextView
    private lateinit var server: EditText
    private lateinit var code: EditText
    private lateinit var toggle: Button
    private val ui = Handler(Looper.getMainLooper())
    private val tick = object : Runnable {
        override fun run() {
            refresh()
            ui.postDelayed(this, 1000)
        }
    }

    private fun text(s: String, size: Float = 15f, bold: Boolean = false) = TextView(this).apply {
        this.text = s
        textSize = size
        if (bold) setTypeface(typeface, android.graphics.Typeface.BOLD)
        setPadding(0, 12, 0, 4)
    }

    private fun button(s: String, onClick: () -> Unit) = Button(this).apply {
        this.text = s
        isAllCaps = false
        setOnClickListener { onClick() }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val col = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(40, 60, 40, 40) }
        col.addView(text("Mobile Agent", 24f, true))
        status = text("", 15f)
        col.addView(status)

        col.addView(text("1. Pair with your server", 17f, true))
        col.addView(text("Open the web app, show the pairing code, and type the server address and code here."))
        server = EditText(this).apply { setText(Prefs.server(this@MainActivity)); hint = "Server, for example http://192.168.1.20:8001"; inputType = InputType.TYPE_TEXT_VARIATION_URI; setSingleLine() }
        code = EditText(this).apply { hint = "Pairing code, for example ABCD-EFGH"; inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_CAP_CHARACTERS; setSingleLine() }
        col.addView(server)
        col.addView(code)
        col.addView(button("Pair") { pair() })

        col.addView(text("2. Allow it to operate the phone", 17f, true))
        col.addView(text("Turn on Mobile Agent under Accessibility. On Android 13 and newer, if the switch is greyed out: open App info, tap the three dots, choose Allow restricted settings, then try again."))
        col.addView(button("Open accessibility settings") { startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)) })
        col.addView(button("Open App info (restricted settings)") {
            startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName")))
        })

        col.addView(text("3. Keep it running in the background", 17f, true))
        col.addView(button("Allow background running (battery)") {
            val pm = getSystemService(PowerManager::class.java)
            if (pm.isIgnoringBatteryOptimizations(packageName)) Toast.makeText(this, "Already allowed", Toast.LENGTH_SHORT).show()
            else startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))
        })
        col.addView(button("Allow display over other apps (helps open apps from the background)") {
            startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName")))
        })

        col.addView(text("4. Run", 17f, true))
        toggle = button("Start agent") { toggleAgent() }
        col.addView(toggle)
        col.addView(button("Forget this server") { AgentService.stop(this); Prefs.forget(this); refresh() })
        col.addView(text("While it runs, the agent can see and operate this screen when you give it a task. The notification has a Stop button; turning off the accessibility service stops it for good.", 13f))

        setContentView(ScrollView(this).apply { addView(col) })
        requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
    }

    override fun onResume() {
        super.onResume()
        if (Prefs.enabled(this) && Prefs.token(this).isNotEmpty()) AgentService.start(this)  // opening the app brings a stopped link back
        ui.post(tick)
    }

    override fun onPause() {
        ui.removeCallbacks(tick)
        super.onPause()
    }

    private fun pair() {
        val s = server.text.toString().trim().trimEnd('/')
        val c = code.text.toString()
        status.text = "Pairing…"
        Thread {
            try {
                val token = Pairing.pair(s, c)
                Prefs.savePairing(this, s, token)
                AgentService.start(this)
                runOnUiThread { code.setText(""); Toast.makeText(this, "Paired", Toast.LENGTH_SHORT).show() }
            } catch (e: Exception) {
                runOnUiThread { Toast.makeText(this, "Could not pair: ${e.message}", Toast.LENGTH_LONG).show() }
            }
        }.start()
    }

    private fun toggleAgent() {
        if (Prefs.enabled(this)) AgentService.stop(this)
        else if (Prefs.token(this).isEmpty()) Toast.makeText(this, "Pair first", Toast.LENGTH_SHORT).show()
        else AgentService.start(this)
        refresh()
    }

    private fun refresh() {
        val paired = Prefs.token(this).isNotEmpty()
        val a11y = AgentAccessibilityService.instance != null
        toggle.text = if (Prefs.enabled(this)) "Stop agent" else "Start agent"
        status.text = buildString {
            append(if (paired) "Paired with ${Prefs.server(this@MainActivity)}" else "Not paired yet")
            append("\nConnection: ").append(if (Prefs.enabled(this@MainActivity)) Status.connection else "stopped")
            append("\nAccessibility service: ").append(if (a11y) "on" else "OFF (step 2)")
            if (Status.lastError.isNotEmpty() && Prefs.enabled(this@MainActivity)) append("\nLast problem: ").append(Status.lastError)
        }
        status.visibility = View.VISIBLE
    }
}
