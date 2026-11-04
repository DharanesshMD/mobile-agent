package ai.mobileagent.client

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.ClipData
import android.content.ClipboardManager
import android.graphics.Bitmap
import android.graphics.Path
import android.graphics.Rect
import android.os.Bundle
import android.view.Display
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityWindowInfo
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * The hands and eyes of the agent: reads the UI tree, takes screenshots, taps, swipes and types on the real screen.
 * Mirrors what `agent/device.py` does through adb, so the server's worker runs unchanged on this phone.
 */
class AgentAccessibilityService : AccessibilityService() {
    companion object {
        @Volatile var instance: AgentAccessibilityService? = null
        private const val SHOT_SPACING_MS = 333L  // Android rejects screenshots closer together than this
    }

    private val shotExecutor = Executors.newSingleThreadExecutor()
    private var lastShot = 0L

    override fun onServiceConnected() {
        instance = this
    }

    override fun onUnbind(intent: android.content.Intent?): Boolean {
        instance = null
        OtpRelay.stop(this)
        NotifFeed.clear()
        return super.onUnbind(intent)
    }

    override fun onDestroy() {
        instance = null
        OtpRelay.stop(this)
        NotifFeed.clear()
        super.onDestroy()
    }

    /**
     * Notification rows that are visible right now on the lock screen or in the open shade, read from the window tree: [app, title, text] each.
     * Best effort (standard Android view ids; a maker's own layout may not match). Needed because with a secure lock Android delivers notification
     * events WITHOUT their text, while the lock screen itself still shows it. Empty unless the screen is on and SystemUI is in front. Never changes the screen.
     */
    fun visibleNotifications(): List<Array<String>> {
        val root = rootInActiveWindow ?: return emptyList()
        if (root.packageName?.toString() != "com.android.systemui") return emptyList()
        fun first(n: AccessibilityNodeInfo, vararg ids: String): String {
            for (id in ids) n.findAccessibilityNodeInfosByViewId(id).firstOrNull()?.text?.toString()?.trim()?.takeIf { it.isNotEmpty() }?.let { return it }
            return ""
        }
        // the open shade and a notification's own layout use android:id/*; the lock screen's grouped rows use SystemUI's notification_* ids
        val titleIds = listOf("android:id/title", "com.android.systemui:id/notification_title")
        val textIds = arrayOf("android:id/big_text", "android:id/text", "com.android.systemui:id/notification_text")
        val out = ArrayList<Array<String>>()
        for (tid in titleIds) for (t in root.findAccessibilityNodeInfosByViewId(tid)) {
            val title = t.text?.toString()?.trim().orEmpty()
            if (title.isEmpty()) continue
            var row = t.parent
            var depth = 0
            while (row != null && depth < 6 && first(row, *textIds).isEmpty()) { row = row.parent; depth++ }
            if (row == null || depth >= 6) continue
            var app = first(row, "android:id/app_name_text")
            var up = row.parent
            var hops = 0
            while (app.isEmpty() && up != null && hops < 4) { app = first(up, "android:id/app_name_text"); up = up.parent; hops++ }
            out.add(arrayOf(app, title, first(row, *textIds)))
        }
        return out.distinctBy { it[1] + "\u0000" + it[2] }
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        // Notification text is read only (a) while the server holds an open watch window for a verification code, or (b) when the owner switched on "read my notifications".
        if (event?.eventType != AccessibilityEvent.TYPE_NOTIFICATION_STATE_CHANGED) return
        OtpRelay.seen++
        val otp = OtpRelay.watching()
        val feed = NotifFeed.enabled(this)
        if (!otp && !feed) { OtpRelay.gated++; return }
        val p = NotifParse.parse(this, event) ?: run { OtpRelay.unparsed++; return }
        OtpRelay.parsed++
        if (otp) OtpRelay.consume(this, p)
        if (feed) NotifFeed.add(this, p)
    }
    override fun onInterrupt() {}

    // --- reading the screen ------------------------------------------------------------------------------------

    fun screenSize(): IntArray {
        val b = getSystemService(android.view.WindowManager::class.java).maximumWindowMetrics.bounds
        return intArrayOf(b.width(), b.height())
    }

    /** PNG of the whole screen. A secure window (banking, passwords) cannot be captured: the caller gets a black frame of the right size. */
    fun screenshotPng(): ByteArray {
        var attempts = 0
        while (true) {
            val wait = lastShot + SHOT_SPACING_MS - System.currentTimeMillis()
            if (wait > 0) Thread.sleep(wait)
            lastShot = System.currentTimeMillis()
            val latch = CountDownLatch(1)
            var bitmap: Bitmap? = null
            var error = 0
            takeScreenshot(Display.DEFAULT_DISPLAY, shotExecutor, object : TakeScreenshotCallback {
                override fun onSuccess(result: ScreenshotResult) {
                    try {
                        bitmap = Bitmap.wrapHardwareBuffer(result.hardwareBuffer, result.colorSpace)?.copy(Bitmap.Config.ARGB_8888, false)
                    } finally {
                        result.hardwareBuffer.close()
                        latch.countDown()
                    }
                }

                override fun onFailure(errorCode: Int) {
                    error = errorCode
                    latch.countDown()
                }
            })
            if (!latch.await(10, TimeUnit.SECONDS)) throw IllegalStateException("the screenshot timed out")
            bitmap?.let { return png(it) }
            when (error) {
                ERROR_TAKE_SCREENSHOT_SECURE_WINDOW -> {
                    val (w, h) = screenSize().let { it[0] to it[1] }
                    return png(Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888).also { it.eraseColor(0xFF000000.toInt()) })
                }
                ERROR_TAKE_SCREENSHOT_INTERVAL_TIME_SHORT -> if (++attempts < 5) continue
            }
            throw IllegalStateException("the screenshot failed (code $error)")
        }
    }

    private fun png(b: Bitmap): ByteArray {
        val out = ByteArrayOutputStream()
        b.compress(Bitmap.CompressFormat.PNG, 100, out)
        return out.toByteArray()
    }

    /** The UI as the worker wants it: labelled, tappable things only, with the same filtering as Device.elements in agent/device.py. */
    fun elements(limit: Int): JSONObject {
        val keyboard = windows.any { it.type == AccessibilityWindowInfo.TYPE_INPUT_METHOD }
        val out = JSONArray()
        var root: AccessibilityNodeInfo? = rootInActiveWindow
        var tries = 0
        while (root == null && tries++ < 6) {  // the root is briefly null while an app is switching windows
            Thread.sleep(150)
            root = rootInActiveWindow
        }
        if (root == null) return JSONObject().put("keyboard", keyboard).put("elements", out)
        val height = screenSize()[1]
        val seen = HashSet<String>()
        val bounds = Rect()
        fun visit(n: AccessibilityNodeInfo, depth: Int) {
            if (out.length() >= limit || depth > 60) return
            val pkg = n.packageName?.toString()?.lowercase().orEmpty()
            val skip = !n.isVisibleToUser || pkg.contains("inputmethod")
            if (!skip) {
                n.getBoundsInScreen(bounds)
                val raw = (n.text?.toString()?.takeIf { it.isNotBlank() } ?: n.contentDescription?.toString()?.takeIf { it.isNotBlank() }).orEmpty()
                val statusBar = pkg == "com.android.systemui" && bounds.bottom <= height * 0.06
                if ((raw.isNotEmpty() || n.isClickable) && bounds.width() > 0 && bounds.height() > 0 && !statusBar) {
                    val cx = (bounds.left + bounds.right) / 2
                    val cy = (bounds.top + bounds.bottom) / 2
                    if (seen.add("$raw|$cx|$cy")) {
                        val label = raw.ifEmpty { n.viewIdResourceName?.substringAfterLast('/').orEmpty() }
                            .ifEmpty { n.className?.toString()?.substringAfterLast('.').orEmpty() }
                        out.put(JSONObject().put("label", label).put("center", JSONArray(listOf(cx, cy)))
                            .put("clickable", n.isClickable).put("state", state(n)))
                    }
                }
            }
            if (pkg.contains("inputmethod")) return
            for (i in 0 until n.childCount) n.getChild(i)?.let { visit(it, depth + 1) }
        }
        visit(root, 0)
        return JSONObject().put("keyboard", keyboard).put("elements", out)
    }

    private fun state(n: AccessibilityNodeInfo): String {
        val bits = ArrayList<String>()
        if (n.isCheckable) bits.add(if (n.isChecked) "on" else "off")
        if (n.isSelected) bits.add("selected")
        if (n.isScrollable) bits.add("scrollable")
        if (n.className?.toString()?.endsWith("EditText") == true) bits.add("text field")
        if (n.isFocused) bits.add("focused")
        if (!n.isEnabled) bits.add("disabled")
        return bits.joinToString(", ")
    }

    fun foregroundPackage(): String = rootInActiveWindow?.packageName?.toString().orEmpty()

    // --- touching the screen -----------------------------------------------------------------------------------

    private fun gesture(path: Path, ms: Long) {
        val latch = CountDownLatch(1)
        var done = false
        val g = GestureDescription.Builder().addStroke(GestureDescription.StrokeDescription(path, 0, ms.coerceAtLeast(1))).build()
        val ok = dispatchGesture(g, object : GestureResultCallback() {
            override fun onCompleted(gestureDescription: GestureDescription?) { done = true; latch.countDown() }
            override fun onCancelled(gestureDescription: GestureDescription?) { latch.countDown() }
        }, null)
        if (!ok) throw IllegalStateException("Android refused the gesture")
        latch.await(ms + 5000, TimeUnit.MILLISECONDS)
        if (!done) throw IllegalStateException("the gesture was cancelled")
    }

    fun tap(x: Int, y: Int) = gesture(Path().apply { moveTo(x.toFloat(), y.toFloat()) }, 50)

    fun swipe(x1: Int, y1: Int, x2: Int, y2: Int, ms: Long) =
        gesture(Path().apply { moveTo(x1.toFloat(), y1.toFloat()); lineTo(x2.toFloat(), y2.toFloat()) }, ms)

    // --- keys and text -----------------------------------------------------------------------------------------

    private fun focusedField(): AccessibilityNodeInfo? =
        rootInActiveWindow?.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
            ?: windows.firstNotNullOfOrNull { it.root?.findFocus(AccessibilityNodeInfo.FOCUS_INPUT) }

    private fun setText(node: AccessibilityNodeInfo, text: String): Boolean {
        val args = Bundle().apply { putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text) }
        return node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
    }

    /** Appends to the focused field (like keyboard input). Falls back to paste when the field does not take SET_TEXT. */
    fun typeText(text: String) {
        val node = focusedField() ?: throw IllegalStateException("no text field is focused: tap one first")
        val current = if (node.isShowingHintText) "" else node.text?.toString().orEmpty()
        val masked = node.isPassword && current.isNotEmpty()  // a password field reports dots, so appending to it would corrupt it
        if (!masked && setText(node, current + text)) return
        val clipboard = getSystemService(ClipboardManager::class.java)
        clipboard.setPrimaryClip(ClipData.newPlainText("text", text))
        if (!node.performAction(AccessibilityNodeInfo.ACTION_PASTE)) throw IllegalStateException("the focused field did not accept the text")
    }

    fun key(name: String) {
        val ok: Boolean = when (name) {
            "KEYCODE_BACK" -> performGlobalAction(GLOBAL_ACTION_BACK)
            "KEYCODE_HOME" -> performGlobalAction(GLOBAL_ACTION_HOME)
            "KEYCODE_APP_SWITCH" -> performGlobalAction(GLOBAL_ACTION_RECENTS)
            "KEYCODE_ENTER" -> {
                val node = focusedField() ?: throw IllegalStateException("no text field is focused, so Enter has nothing to do")
                node.performAction(AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.id)
            }
            "KEYCODE_DEL" -> {
                val node = focusedField() ?: throw IllegalStateException("no text field is focused")
                val current = node.text?.toString().orEmpty()
                current.isEmpty() || setText(node, current.dropLast(1))
            }
            "KEYCODE_SPACE" -> { typeText(" "); true }
            else -> throw IllegalArgumentException("key $name is not supported on a real phone")
        }
        if (!ok) throw IllegalStateException("Android refused $name")
    }
}
