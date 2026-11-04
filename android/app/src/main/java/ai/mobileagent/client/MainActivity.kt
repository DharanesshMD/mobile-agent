package ai.mobileagent.client

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.app.AppOpsManager
import android.app.NotificationManager
import android.content.ComponentName
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast

/**
 * One screen. A setup checklist on top (every switch the app needs, each with a plain reason and a button that opens exactly the Settings screen where
 * it is turned on), then pairing, run and history. The list is re-checked when the owner comes back from Settings and every 2 seconds while visible.
 * Nothing is switched on silently: the owner flips every switch.
 */
class MainActivity : Activity() {
    private enum class State { DONE, TODO, ATTENTION }

    /** One requirement: how to tell it is met, why it is needed, and where to go to fix it. */
    private inner class Req(
        val id: String, val title: String, val why: String, val button: String,
        val check: () -> State, val go: () -> Unit, val help: String = "",
        val visible: () -> Boolean = { true },
        /** MIUI screens cannot be read back: the owner says when they are done (stored in Prefs). */
        val manual: Boolean = false,
        /** An extra the owner may switch on: shown in the list but it never blocks "All set". */
        val optional: Boolean = false,
        /** A switch the owner turns on and off (the button reads Turn on / Turn off and stays visible). */
        val toggle: Boolean = false,
    ) {
        lateinit var box: LinearLayout
        lateinit var mark: TextView
        lateinit var titleView: TextView
        lateinit var helpView: TextView
        lateinit var action: Button
        lateinit var done: Button
        var state = State.TODO
    }

    private lateinit var status: TextView
    private lateinit var server: EditText
    private lateinit var code: EditText
    private lateinit var toggle: Button
    private lateinit var scroll: ScrollView
    private lateinit var summary: TextView
    private lateinit var nextBtn: Button
    private lateinit var checklist: LinearLayout
    private lateinit var pairBox: LinearLayout
    private lateinit var showSetup: Button
    private lateinit var found: TextView
    private lateinit var candidates: LinearLayout
    private lateinit var findBtn: Button
    private var search: Discovery.Search? = null
    private var searchId = 0  // a result that belongs to an older search is ignored
    private var autoUrl = ""  // the address discovery put in the field: only that is replaced by a later search, never what the owner typed
    private var fromLink = false  // the pairing link carried the server: no search
    private val reqs = mutableListOf<Req>()
    private var setupShown = false
    private val ui = Handler(Looper.getMainLooper())
    private val tick = object : Runnable {
        override fun run() {
            refresh()
            ui.postDelayed(this, 2000)
        }
    }

    private val pkg get() = packageName
    /** Xiaomi, Redmi and POCO phones (MIUI, HyperOS). The "fake_miui" flag lets a test show these rows on another phone. */
    private val miui get() = Prefs.flag(this, "fake_miui") || Build.MANUFACTURER.lowercase().let { it == "xiaomi" || it == "redmi" || it == "poco" }

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    private fun text(s: String, size: Float = 15f, bold: Boolean = false) = TextView(this).apply {
        this.text = s
        textSize = size
        if (bold) setTypeface(typeface, android.graphics.Typeface.BOLD)
        setPadding(0, dp(6), 0, dp(2))
    }

    private fun button(s: String, onClick: () -> Unit) = Button(this).apply {
        this.text = s
        isAllCaps = false
        setOnClickListener { onClick() }
    }

    private fun details() = Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$pkg"))

    /** Open a Settings screen; if this phone does not have it (or refuses), fall back to the app's own details page and say what to do there. */
    private fun open(intent: Intent, fallbackHelp: String) {
        try {
            startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        } catch (e: Exception) {  // ActivityNotFoundException, SecurityException (a vendor screen that is not exported)
            Toast.makeText(this, fallbackHelp, Toast.LENGTH_LONG).show()
            try { startActivity(details()) } catch (_: Exception) { startActivity(Intent(Settings.ACTION_SETTINGS)) }
        }
    }

    // ---- what is checked -------------------------------------------------------------------------------------------------------------------------
    private fun paired() = Prefs.token(this).isNotEmpty()

    /** Our accessibility service is in the enabled list (what Settings shows as On), or is already running. */
    private fun a11yOn(): Boolean {
        if (AgentAccessibilityService.instance != null) return true
        val me = ComponentName(this, AgentAccessibilityService::class.java)
        val list = Settings.Secure.getString(contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES).orEmpty()
        return Settings.Secure.getInt(contentResolver, Settings.Secure.ACCESSIBILITY_ENABLED, 0) == 1 &&
            list.split(':').any { ComponentName.unflattenFromString(it) == me }
    }

    /** Android 13+: an app that was not installed from a store has "restricted settings", which greys out its accessibility switch until the owner allows it. */
    private fun restricted(): Boolean {
        if (Build.VERSION.SDK_INT < 33) return false
        return try {
            val ops = getSystemService(AppOpsManager::class.java)
            ops.unsafeCheckOpNoThrow("android:access_restricted_settings", applicationInfo.uid, pkg) == AppOpsManager.MODE_IGNORED
        } catch (_: Exception) {
            false  // the check is not available on this build: the help row stays available through "I tried and the switch is greyed out"
        }
    }

    private fun notificationsOn(): Boolean =
        if (Build.VERSION.SDK_INT >= 33) checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
        else getSystemService(NotificationManager::class.java).areNotificationsEnabled()

    private fun batteryOk(): Boolean = getSystemService(PowerManager::class.java).isIgnoringBatteryOptimizations(pkg)

    private fun manualState(id: String) = if (Prefs.flag(this, "done_$id")) State.DONE else State.TODO

    private fun buildRequirements() {
        reqs += Req("paired", "Pair with your server",
            "Links this phone to the computer where your assistant runs. Scan the QR code in the web app (Connect a phone) and tap Open in the app, or type the address and code below.",
            "Pair below", { if (paired()) State.DONE else State.TODO }, { scroll.post { scroll.smoothScrollTo(0, pairBox.top) }; server.requestFocus() })

        // Android 13+: when the switch is greyed out this must come first, so it is placed before the accessibility row and shown as "!" once the owner has tried.
        reqs += Req("restricted", "Allow restricted settings",
            "Android blocks the accessibility switch for apps installed outside a store until you allow it here. It is a one-time step.",
            "Open App info",
            { if (a11yOn()) State.DONE else if (restricted() || Prefs.flag(this, "a11y_opened")) State.ATTENTION else State.TODO },
            { open(details(), "Open Settings > Apps > Mobile Agent") },
            help = "In App info tap the three dots at the top right, choose Allow restricted settings and confirm with your PIN or fingerprint. Then come back and turn on the accessibility service below.",
            visible = { Build.VERSION.SDK_INT >= 33 && !a11yOn() })

        reqs += Req("a11y", "Turn on the accessibility service",
            "Lets the assistant read and tap this screen for tasks you give it. It does nothing until you are connected, and turning it off stops it for good.",
            "Turn on", { if (a11yOn()) State.DONE else State.TODO },
            { Prefs.setFlag(this, "a11y_opened", true); open(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS), "Open Settings > Accessibility") },
            help = "In the list open Installed apps (or Downloaded apps), tap Mobile Agent and switch it on.")

        reqs += Req("notifications", "Allow notifications",
            "Android requires a notification while the app holds the connection, and it carries the Stop button.",
            "Allow", { if (notificationsOn()) State.DONE else State.TODO },
            {
                if (Build.VERSION.SDK_INT >= 33 && !Prefs.flag(this, "notif_asked")) {
                    Prefs.setFlag(this, "notif_asked", true)
                    requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
                } else {
                    open(Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE, pkg), "Open Settings > Notifications > Mobile Agent")
                }
            })

        reqs += Req("battery", "Run in the background",
            "Without this Android may put the connection to sleep and the phone stops answering tasks.",
            "Allow", { if (batteryOk()) State.DONE else State.TODO },
            {
                open(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$pkg")), "Open Settings > Battery > Mobile Agent and choose No restrictions")
            })

        // SYSTEM_ALERT_WINDOW is used implicitly: Android lets an app that may display over other apps start an activity from the background, which
        // the open_app, open_url and open_intent requests need when another app is in front (Ops.launch runs from a service).
        reqs += Req("overlay", "Display over other apps",
            "Android only lets the assistant open an app for you while this app is in the background if this is allowed.",
            "Allow", { if (Settings.canDrawOverlays(this)) State.DONE else State.TODO },
            { open(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$pkg")), "Open Settings > Apps > Mobile Agent > Display over other apps") })

        // Xiaomi, Redmi, POCO (MIUI and HyperOS): the vendor kills background apps unless these are set. They cannot be read back, so the owner taps Done.
        reqs += Req("miui_autostart", "Autostart (Xiaomi)",
            "MIUI stops apps in the background unless Autostart is on, which would drop the connection.",
            "Open and check",
            { manualState("miui_autostart") },
            {
                open(Intent().setComponent(ComponentName("com.miui.securitycenter", "com.miui.permcenter.autostart.AutoStartManagementActivity")),
                    "In App info open Autostart and switch it on for Mobile Agent")
            },
            help = "Switch Mobile Agent on in the list, then tap Done here.", visible = { miui }, manual = true)

        reqs += Req("miui_popup", "Pop-up windows in the background (Xiaomi)",
            "MIUI blocks an app from opening other apps while it runs in the background unless this permission is allowed.",
            "Open and check",
            { manualState("miui_popup") },
            {
                open(Intent("miui.intent.action.APP_PERM_EDITOR").setComponent(ComponentName("com.miui.securitycenter", "com.miui.permcenter.permissions.PermissionsEditorActivity"))
                    .putExtra("extra_pkgname", pkg), "In App info open Other permissions and allow Display pop-up windows while running in the background")
            },
            help = "Find Display pop-up windows while running in the background and choose Allow, then tap Done here.", visible = { miui }, manual = true)

        reqs += Req("miui_battery", "Battery saver: No restrictions (Xiaomi)",
            "MIUI's own battery saver can freeze the app even when Android's battery setting is allowed.",
            "Open and check",
            { manualState("miui_battery") },
            {
                open(Intent().setComponent(ComponentName("com.miui.powerkeeper", "com.miui.powerkeeper.ui.HiddenAppsConfigActivity"))
                    .putExtra("package_name", pkg).putExtra("package_label", getString(R.string.app_name)),
                    "In App info open Battery saver and choose No restrictions")
            },
            help = "Choose No restrictions, then tap Done here.", visible = { miui }, manual = true)

        // Optional: verification codes for tasks on a cloud phone. Off unless the owner turns it on; read nothing unless a task is waiting for a code (OtpRelay).
        reqs += Req("otp", "Let tasks fetch verification codes from this phone",
            "Only while a task is waiting for a code, and only after you approve each code on your computer; the code never leaves unless you approve. It reads the text of one incoming notification at a time and keeps nothing.",
            "Turn on",
            { if (Prefs.flag(this, OtpRelay.FLAG)) State.DONE else State.TODO },
            { Prefs.setFlag(this, OtpRelay.FLAG, !Prefs.flag(this, OtpRelay.FLAG)); if (!Prefs.flag(this, OtpRelay.FLAG)) OtpRelay.stop(this); refresh() },
            help = "This needs the accessibility service above to be on, and the message text must be visible in the notification (not hidden on the lock screen).",
            optional = true, toggle = true)

        // Optional: "what did I miss". Memory only, 50 notifications, 24 hours, verification codes hidden (NotifFeed).
        reqs += Req("notif_read", "Let the assistant read my notifications",
            "So it can tell you what you missed, even when the phone is locked. It keeps the last 50 notifications in memory for up to 24 hours, hides any verification code, and forgets them when you turn this off.",
            "Turn on",
            { if (Prefs.flag(this, NotifFeed.FLAG)) State.DONE else State.TODO },
            { Prefs.setFlag(this, NotifFeed.FLAG, !Prefs.flag(this, NotifFeed.FLAG)); if (!Prefs.flag(this, NotifFeed.FLAG)) NotifFeed.clear(); refresh() },
            help = "This needs the accessibility service above to be on. The phone can wake and show what its lock screen shows, but it can never unlock the phone; whether the text is visible while locked depends on Settings > Notifications > Lock screen (on Xiaomi also Notifications > Show on lock screen).",
            optional = true, toggle = true)
    }

    // ---- the screen -----------------------------------------------------------------------------------------------------------------------------
    private fun rowView(r: Req): View {
        val card = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(dp(14), dp(12), dp(14), dp(12))
            background = GradientDrawable().apply { cornerRadius = dp(12).toFloat(); setColor(Color.argb(28, 128, 128, 128)) }
            layoutParams = LinearLayout.LayoutParams(-1, -2).apply { setMargins(0, dp(6), 0, dp(6)) }
        }
        r.mark = TextView(this).apply { textSize = 22f; gravity = Gravity.CENTER_HORIZONTAL; minWidth = dp(34) }
        val col = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; layoutParams = LinearLayout.LayoutParams(0, -2, 1f) }
        r.titleView = text(r.title, 16f, true).apply { setPadding(0, 0, 0, dp(2)) }
        val why = text(r.why, 13f).apply { setPadding(0, 0, 0, dp(2)) }
        r.helpView = text(r.help, 13f).apply { setTextColor(Color.rgb(0xE6, 0x51, 0x00)); visibility = View.GONE }
        r.action = button(r.button) { r.go() }
        r.done = button("Done") { Prefs.setFlag(this, "done_${r.id}", !Prefs.flag(this, "done_${r.id}")); refresh() }
        col.addView(r.titleView); col.addView(why)
        if (r.help.isNotEmpty()) col.addView(r.helpView)
        val actions = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        actions.addView(r.action)
        if (r.manual) actions.addView(r.done)
        col.addView(actions)
        if (r.id == "paired") {
            pairBox = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
            found = text("", 14f).apply { setTextColor(Color.GRAY) }
            candidates = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
            findBtn = button("Find my computer") { fromLink = false; startSearch() }
            server = EditText(this).apply { setText(Prefs.server(this@MainActivity)); hint = "Server address (filled in when your computer is found)"; inputType = InputType.TYPE_TEXT_VARIATION_URI; setSingleLine() }
            code = EditText(this).apply { hint = "Pairing code, for example ABCD-EFGH"; inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_CAP_CHARACTERS; setSingleLine() }
            pairBox.addView(found); pairBox.addView(candidates)
            pairBox.addView(LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                addView(findBtn)
                addView(button("Enter the address yourself") { autoUrl = ""; server.setText(""); server.requestFocus() })
            })
            pairBox.addView(server); pairBox.addView(code); pairBox.addView(button("Pair") { pair() })
            col.addView(pairBox)
        }
        card.addView(r.mark); card.addView(col)
        r.box = card
        return card
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val col = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(16), dp(28), dp(16), dp(24)) }
        col.addView(text("Mobile Agent", 24f, true))
        status = text("", 14f)
        col.addView(status)

        summary = text("", 17f, true)
        col.addView(summary)
        nextBtn = button("Next step") { nextStep() }.apply { textSize = 17f; setPadding(dp(8), dp(14), dp(8), dp(14)) }
        col.addView(nextBtn)
        showSetup = button("Show setup list") { setupShown = !setupShown; refresh() }
        col.addView(showSetup)

        buildRequirements()
        checklist = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        val rowsFor = reqs.map { rowView(it) }
        rowsFor.forEach { checklist.addView(it) }
        col.addView(checklist)

        col.addView(text("Run", 17f, true))
        toggle = button("Start agent") { toggleAgent() }
        col.addView(toggle)
        col.addView(button("History: what the assistant did here") { startActivity(Intent(this, HistoryActivity::class.java)) })
        col.addView(button("Disconnect and revoke") { disconnect() })
        col.addView(text("While it runs, the agent can see and operate this screen when you give it a task. The notification has a Stop button; turning off the accessibility service stops it for good.", 13f))

        scroll = ScrollView(this).apply { addView(col) }
        setContentView(scroll)
        handleLink(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleLink(intent)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        refresh()
    }

    /** mobileagent://pair?server=...&code=...: fill the fields in and ask once; nothing happens until the owner taps Confirm. */
    private fun handleLink(i: Intent?) {
        val u = i?.data ?: return
        if (u.scheme != "mobileagent" || u.host != "pair") return
        setIntent(Intent())  // handled once
        val srv = u.getQueryParameter("server")?.trim().orEmpty()
        val cd = u.getQueryParameter("code")?.trim().orEmpty()
        if (!(srv.startsWith("http://") || srv.startsWith("https://")) || cd.length < 4) {
            Toast.makeText(this, "That pairing link is not valid", Toast.LENGTH_LONG).show()
            return
        }
        fromLink = true  // the link names the server: nothing to look for
        stopSearch()
        autoUrl = ""
        found.text = ""
        candidates.removeAllViews()
        server.setText(srv)
        code.setText(cd)
        AlertDialog.Builder(this)
            .setTitle("Pair this phone?")
            .setMessage("Connect to $srv. The assistant on that computer will be able to operate this phone when you ask it to. You can disconnect any time.")
            .setPositiveButton("Confirm") { _, _ -> pair() }
            .setNegativeButton("Cancel", null)
            .show()
    }

    /** Tell the server to forget this phone, then forget the token here and stop. */
    private fun disconnect() {
        val s = Prefs.server(this)
        val t = Prefs.token(this)
        AgentService.stop(this)
        Thread {
            val msg = try {
                if (t.isNotEmpty() && s.isNotEmpty()) Pairing.revoke(s, t)
                "Disconnected. The server no longer accepts this phone."
            } catch (e: Exception) {
                "Disconnected here. The server could not be reached to revoke: ${e.message}"
            }
            Prefs.forget(this)
            runOnUiThread { Toast.makeText(this, msg, Toast.LENGTH_LONG).show(); refresh() }
        }.start()
    }

    override fun onResume() {
        super.onResume()
        if (Prefs.enabled(this) && Prefs.token(this).isNotEmpty()) AgentService.start(this)  // opening the app brings a stopped link back
        refresh()  // back from Settings: update at once
        ui.postDelayed(tick, 2000)
        if (!paired() && !fromLink) startSearch()  // the pairing part is open: look for the computer
    }

    override fun onPause() {
        ui.removeCallbacks(tick)
        stopSearch()
        super.onPause()
    }

    // ---- finding the computer ------------------------------------------------------------------------------------------------------------------
    private fun stopSearch() {
        search?.cancel()
        search = null
        searchId++
    }

    private fun startSearch() {
        stopSearch()
        found.text = "Looking for your computer on this Wi-Fi…"
        candidates.removeAllViews()
        findBtn.isEnabled = false
        val id = ++searchId
        val me = Discovery.Search(applicationContext) { r -> runOnUiThread { if (id == searchId) showResult(r) } }
        search = me
        me.start()
    }

    private fun showResult(r: Discovery.Result) {
        search = null
        findBtn.isEnabled = true
        findBtn.text = if (r.found.isEmpty()) "Retry" else "Find my computer"
        candidates.removeAllViews()
        when {
            r.noWifi -> {
                found.text = "Wi-Fi is off or not connected. Turn it on and join the same Wi-Fi as your computer, then tap Retry."
                candidates.addView(button("Open Wi-Fi settings") { open(Intent(Settings.ACTION_WIFI_SETTINGS), "Open Settings > Wi-Fi") })
            }
            r.found.isEmpty() ->
                found.text = "Could not find your computer. Is Allow phones on this Wi-Fi switched on, and is the phone on the same Wi-Fi? You can also enter the address yourself."
            r.found.size == 1 -> {
                val f = r.found[0]
                found.text = "✓ Found ${f.name}"
                use(f)
            }
            else -> {
                found.text = "Found ${r.found.size} computers. Pick yours:"
                r.found.forEach { f -> candidates.addView(button("${f.name} (${f.ip})") { found.text = "✓ Found ${f.name}"; candidates.removeAllViews(); use(f) }) }
            }
        }
    }

    /** Put the address in the field, unless the owner has typed a different one (a field they did not touch, or one discovery filled before, is replaced). */
    private fun use(f: Discovery.Found) {
        val now = server.text.toString().trim()
        if (now.isEmpty() || now == autoUrl || now == Prefs.server(this)) {
            autoUrl = f.url
            server.setText(f.url)
        }
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
                runOnUiThread {
                    code.setText("")
                    Toast.makeText(this, "Paired. Now the next step.", Toast.LENGTH_SHORT).show()
                    scroll.fullScroll(View.FOCUS_UP)  // land on the checklist
                    refresh()
                }
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

    private fun shown() = reqs.filter { it.visible() }

    private fun nextStep() {
        val r = shown().firstOrNull { !it.optional && it.check() != State.DONE }
        if (r == null) { Toast.makeText(this, "All set", Toast.LENGTH_SHORT).show(); return }
        setupShown = true
        refresh()
        r.go()
    }

    private fun refresh() {
        if (!this::checklist.isInitialized) return
        val visible = shown()
        // "Allow restricted settings" is shown before the accessibility row only when it needs attention; otherwise it stays below it as the help for a greyed-out switch.
        val states = visible.associateWith { it.check() }
        val required = visible.filter { !it.optional }  // the optional extras are listed but never count towards All set
        val missing = required.filter { states[it] != State.DONE }
        val allDone = missing.isEmpty()
        for (r in reqs) {
            val on = r in visible
            r.box.visibility = if (on) View.VISIBLE else View.GONE
            if (!on) continue
            val s = states[r]!!
            r.state = s
            r.mark.text = when (s) { State.DONE -> "✓"; State.ATTENTION -> "!"; State.TODO -> "○" }
            r.mark.setTextColor(when (s) { State.DONE -> Color.rgb(0x2E, 0x7D, 0x32); State.ATTENTION -> Color.rgb(0xE6, 0x51, 0x00); State.TODO -> Color.GRAY })
            r.helpView.visibility = if (r.help.isNotEmpty() && (s != State.DONE || (r.toggle && !a11yOn()))) View.VISIBLE else View.GONE
            r.action.visibility = if ((s == State.DONE && !r.manual && !r.toggle) || r.id == "paired") View.GONE else View.VISIBLE  // pairing has its own button in the row
            if (r.toggle) r.action.text = if (s == State.DONE) "Turn off" else "Turn on"
            if (r.manual) r.done.text = if (s == State.DONE) "Undo done" else "Done"
        }
        // "restricted settings" goes above the accessibility row when it needs attention (checked by the order of the boxes in the list)
        val restricted = reqs.first { it.id == "restricted" }
        val a11yRow = reqs.first { it.id == "a11y" }
        val parent = checklist
        val wantFirst = restricted.state == State.ATTENTION && restricted in visible
        val ri = parent.indexOfChild(restricted.box)
        val ai = parent.indexOfChild(a11yRow.box)
        if (wantFirst && ri > ai) { parent.removeView(restricted.box); parent.addView(restricted.box, ai) }
        if (!wantFirst && ri < ai) { parent.removeView(restricted.box); parent.addView(restricted.box, parent.indexOfChild(a11yRow.box) + 1) }
        pairBox.visibility = if (paired()) View.GONE else View.VISIBLE

        val doneCount = required.size - missing.size
        summary.text = if (allDone) "All set ✓" else "Setup: $doneCount of ${required.size} done"
        nextBtn.visibility = if (allDone) View.GONE else View.VISIBLE
        nextBtn.text = missing.firstOrNull()?.let { "Next step: ${it.title}" } ?: "Next step"
        showSetup.visibility = if (allDone) View.VISIBLE else View.GONE
        showSetup.text = if (setupShown) "Hide setup list" else "Show setup list"
        checklist.visibility = if (!allDone || setupShown) View.VISIBLE else View.GONE

        val a11y = a11yOn()
        toggle.text = if (Prefs.enabled(this)) "Stop agent" else "Start agent"
        status.text = buildString {
            append(if (paired()) "Paired with ${Prefs.server(this@MainActivity)}" else "Not paired yet")
            append("\nConnection: ").append(if (Prefs.enabled(this@MainActivity)) Status.connection else "stopped")
            append("\nAccessibility service: ").append(if (a11y) "on" else "off")
            if (Status.lastError.isNotEmpty() && Prefs.enabled(this@MainActivity)) append("\nLast problem: ").append(Status.lastError)
        }
    }
}
