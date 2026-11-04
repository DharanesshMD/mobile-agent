package ai.mobileagent.client

import android.app.Notification
import android.content.Context
import android.view.accessibility.AccessibilityEvent

/** What the accessibility service learns from one notification event (TYPE_NOTIFICATION_STATE_CHANGED). Shared by OtpRelay and NotifFeed; never persisted or logged. */
object NotifParse {
    class Parsed(
        val pkg: String, val label: String, val title: String, val text: String, val body: String, val atMs: Long,
        val messageLike: Boolean, val ongoing: Boolean, val hidden: Boolean, val key: String,
    )

    /** The system's stand-ins when it hides a notification's content (lock screen settings, Android 15 redaction of one-time codes). */
    private val PLACEHOLDER = Regex("""(?i)sensitive notification content hidden|content (is |was )?hidden|contents? hidden|hidden by (your )?(lock ?screen|policy)|^(new )?messages?$|^\d+ new messages?$""")
    private val MESSAGE_PACKAGES = listOf("messaging", "mms", "sms", "messenger", "whatsapp", "telegram", "signal")
    private val CALL_OR_TIMER = Regex("""(?i)call|calling|incoming|timer|alarm|stopwatch""")

    /**
     * Null for a toast or our own notification. While the phone is locked Android may deliver the event without the notification object (or with only a
     * public stand-in): then the event's own text is used, and when that is empty too the entry is marked hidden (the phone is hiding the details).
     */
    fun parse(c: Context, e: AccessibilityEvent): Parsed? {
        val pkg = e.packageName?.toString().orEmpty()
        if (pkg.isEmpty() || pkg == c.packageName) return null  // our own "watching" and "controlling" notices
        val n = e.parcelableData as? Notification
        if (n == null && e.className?.toString()?.contains("Toast") == true) return null
        val ex = n?.extras
        fun s(cs: CharSequence?) = cs?.toString()?.trim().orEmpty()
        val label = runCatching { c.packageManager.getApplicationLabel(c.packageManager.getApplicationInfo(pkg, 0)).toString() }.getOrDefault(pkg)
        val fromEvent = e.text?.map { s(it) }?.filter { it.isNotEmpty() }.orEmpty()
        val title = s(ex?.getCharSequence(Notification.EXTRA_TITLE)).ifEmpty { if (n == null) fromEvent.firstOrNull().orEmpty() else "" }
        val big = s(ex?.getCharSequence(Notification.EXTRA_BIG_TEXT))
        val plain = s(ex?.getCharSequence(Notification.EXTRA_TEXT))
        val lines = ex?.getCharSequenceArray(Notification.EXTRA_TEXT_LINES)?.map { s(it) }?.filter { it.isNotEmpty() }.orEmpty()
        val sub = s(ex?.getCharSequence(Notification.EXTRA_SUB_TEXT))
        val text = listOf(big, plain, lines.joinToString("\n")).firstOrNull { it.isNotEmpty() }
            ?: fromEvent.filter { it != title }.joinToString("\n")
        val all = LinkedHashSet<String>().apply { listOf(title, big, plain, sub).forEach { if (it.isNotEmpty()) add(it) }; addAll(lines); addAll(fromEvent) }
        val body = all.joinToString("\n")
        val messageLike = n?.category == Notification.CATEGORY_MESSAGE || MESSAGE_PACKAGES.any { pkg.contains(it) }
        val ongoing = n != null && (n.flags and (Notification.FLAG_ONGOING_EVENT or Notification.FLAG_FOREGROUND_SERVICE)) != 0 && !CALL_OR_TIMER.containsMatchIn(title)
        val hidden = body.isEmpty() || PLACEHOLDER.containsMatchIn(text.trim()) || PLACEHOLDER.containsMatchIn(body.trim()) ||
            (text.isEmpty() && title.isNotEmpty() && (messageLike || n?.publicVersion != null))
        // An accessibility event carries neither the notification id nor its tag, so an update is recognised by app + title + post time (or the text when the app sets no time).
        val key = "$pkg|${title.ifEmpty { label }}|" + (if (n != null && n.`when` > 0) n.`when`.toString() else text.hashCode().toString())
        return Parsed(pkg, label, title.ifEmpty { label }, text, body, System.currentTimeMillis(), messageLike, ongoing, hidden, key)
    }
}
