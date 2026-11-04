package ai.mobileagent.client

/**
 * Finds a one-time verification code in the text of a notification. Pure functions, no Android types, no logging:
 * the text and the code never leave the caller. `selfTest()` runs a table of cases (debug builds call it through DebugReceiver).
 *
 * A code is a standalone 4 to 8 digit number (or 3+3 digits split by a space or hyphen) that sits near a cue word. A year, an amount with a
 * currency, a time, a date, a phone number and a reference number without a cue are rejected.
 */
object OtpExtract {
    /** Words that say "this number is a verification code". */
    private val STRONG = listOf(
        "code", "otp", "verification", "verify", "passcode", "pass code", "pin", "one-time", "one time", "security code", "login", "log in", "sign in", "signin",
        "código", "codigo", "code de", "kodu", "kod", "ओटीपी", "कोड", "رمز", "验证码", "認証コード",
    )
    /** Words that only help when a strong cue is somewhere in the same text ("use 483921 to sign in" is a code, "use 1234 points" is not). */
    private val WEAK = listOf("is your", "use ", "enter ")
    private val CURRENCY_BEFORE = Regex("""(?i)(\b(rs\.?|inr|usd|eur|gbp|aed)|[₹$€£¥])\s?$""")
    private val CURRENCY_AFTER = Regex("""(?i)^\s?(rs\b|inr\b|usd\b|eur\b|gbp\b|dollars?\b|rupees?\b|euros?\b|₹|\$|€|£|¥|%|km\b|kg\b|min\b|mins\b|minutes\b|days\b|points?\b|pts\b)""")
    private val NUMBER = Regex("""(?<![\p{L}\p{N}_+.,/@:#$])(\d{4,8}|\d{3}[ -]\d{3})(?![\p{L}\p{N}_/@:]|[.,]\d)""")
    private const val NEAR_BEFORE = 40
    private const val NEAR_AFTER = 25

    private data class Hit(val code: String, val start: Int, val end: Int)

    /** Every number in `t` that counts as a code, best first (empty when there is none). */
    private fun accepted(t: String, trusted: Boolean): List<Hit> {
        val low = t.lowercase()
        val strongAnywhere = STRONG.any { low.contains(it) }
        val weakAnywhere = WEAK.any { low.contains(it) }
        if (!strongAnywhere && !(trusted && weakAnywhere)) return emptyList()  // no cue at all: an order number, a delivery code of a courier, a price
        val hits = NUMBER.findAll(t).map { Hit(it.groupValues[1].replace(" ", "").replace("-", ""), it.groups[1]!!.range.first, it.groups[1]!!.range.last + 1) }
            .filter { plausible(t, low, it) }.toList()
        if (hits.isEmpty()) return emptyList()
        val near = hits.filter { cueDistance(low, it, strongAnywhere) != null }
        val pool = when {
            near.isNotEmpty() -> near
            trusted && hits.size == 1 -> hits  // the only number in an SMS-style notification that mentions a cue
            else -> return emptyList()
        }
        return pool.sortedWith(compareBy<Hit>({ cueDistance(low, it, strongAnywhere) ?: 999 }, { if (it.code.length == 6) 0 else if (it.code.length == 4) 1 else 2 }, { it.start }))
    }

    /** The most likely code in `text`, or null. `trusted` is true for a messaging or banking style notification (an SMS app): then one lone number is enough when any cue is present. */
    fun extract(text: String, trusted: Boolean = false): String? = accepted(text.replace('\u00a0', ' '), trusted).firstOrNull()?.code

    /** `text` with every verification code replaced by "[code]" (what the assistant may see of a notification: never the digits). */
    fun maskCodes(text: String, trusted: Boolean = false): String {
        val t = text.replace('\u00a0', ' ')
        var out = t
        for (h in accepted(t, trusted).sortedByDescending { it.start }) out = out.substring(0, h.start) + "[code]" + out.substring(h.end)
        return out
    }

    /** Distance in characters to the nearest cue, or null when no cue is near enough. */
    private fun cueDistance(low: String, h: Hit, strongAnywhere: Boolean): Int? {
        val cues = if (strongAnywhere) STRONG + WEAK else STRONG
        var best: Int? = null
        for (c in cues) {
            var i = low.indexOf(c)
            while (i >= 0) {
                val end = i + c.length
                val d = when {
                    end <= h.start -> h.start - end  // the cue comes first: "your code is 483921"
                    i >= h.end -> i - h.end  // the number comes first: "483921 is your code"
                    else -> 0
                }
                val limit = if (end <= h.start) NEAR_BEFORE else NEAR_AFTER
                if (d <= limit && (best == null || d < best)) best = d
                i = low.indexOf(c, i + 1)
            }
        }
        return best
    }

    private fun plausible(t: String, low: String, h: Hit): Boolean {
        val before = t.substring(maxOf(0, h.start - 6), h.start)
        val after = t.substring(h.end, minOf(t.length, h.end + 10))
        if (CURRENCY_BEFORE.containsMatchIn(before) || CURRENCY_AFTER.containsMatchIn(after)) return false
        // a phone number: another digit group right next to it ("98765 43210", "044-2345 6789"), unless this is the 3+3 code shape itself
        val grouped = h.code.length == 6 && t.substring(h.start, h.end).any { it == ' ' || it == '-' }
        if (!grouped && (Regex("""\d[ -]$""").containsMatchIn(before) || Regex("""^[ -]\d{3}""").containsMatchIn(after))) return false
        if (grouped && (Regex("""\d[ -]$""").containsMatchIn(before) || Regex("""^[ -]\d""").containsMatchIn(after))) return false
        if (Regex("""(?i)(tel|call|phone|ph|mob|mobile|whatsapp)\W{0,3}$""").containsMatchIn(t.substring(maxOf(0, h.start - 10), h.start))) return false
        // a year is only a code when the cue is right in front of it ("your code is 2024")
        val year = h.code.length == 4 && h.code.toInt() in 1900..2100
        if (year && !Regex("""(?i)(code|otp|pin|passcode|verification|is|:)\s*$""").containsMatchIn(t.substring(maxOf(0, h.start - 14), h.start))) return false
        return true
    }

    // ---- self test ----------------------------------------------------------------------------------------------------------------------------
    private data class Case(val text: String, val want: String?, val trusted: Boolean = false)

    private val CASES = listOf(
        Case("Your code is 483921. Do not share it.", "483921"),
        Case("483921 is your verification code for MyBank", "483921"),
        Case("Use 7421 as your OTP to log in", "7421"),
        Case("OTP: 5528 for txn of Rs. 4500. Valid for 10 min", "5528"),
        Case("Your Amazon OTP is 912345. Do not share it with anyone.", "912345"),
        Case("G-123456 is your Google verification code", "123456"),
        Case("Your security code is 123 456", "123456"),
        Case("Verification code: 48392017", "48392017"),
        Case("Tu código de verificación es 664120", "664120"),
        Case("Votre code de vérification est 302918", "302918"),
        Case("Doğrulama kodu: 771204", "771204"),
        Case("आपका ओटीपी 884312 है", "884312"),
        Case("Enter 6 digits: 204816 to sign in", "204816"),
        Case("Your code is 2024", "2024"),
        Case("Code 9876 expires in 5 minutes", "9876"),
        Case("Uber: 5273 is your Uber code. Never share it.", "5273"),
        Case("Please verify. Reference 5555 8888 code 4412", "4412"),
        // false positives that must stay null
        Case("Your order 123456 shipped", null),
        Case("Call +91 98765 43210 for support", null),
        Case("Call us on 98765 43210. Your OTP is 6643", "6643"),
        Case("Your balance is Rs. 482100 as of 12/05/2024", null),
        Case("Meeting at 10:45 in room 2048", null),
        Case("Happy new year 2025!", null),
        Case("Use 1234 points on your next order", null),
        Case("Tracking 1Z999AA10123456784", null),
        Case("Verification needed: open the app to continue", null),
        Case("Price drop: now \$4999 only, code SAVE20", null),
        Case("Your code is 12345678901", null),
        Case("Lone number 5566 from a message app, use it", "5566", trusted = true),
        Case("Lone number 5566 from a message app", null, trusted = true),
    )

    private val MASKS = listOf(
        "Your code is 483921. Do not share it." to "Your code is [code]. Do not share it.",
        "OTP: 5528 for txn of Rs. 4500. Valid for 10 min" to "OTP: [code] for txn of Rs. 4500. Valid for 10 min",
        "G-123456 is your Google verification code" to "G-[code] is your Google verification code",
        "Meeting at 10:45 in room 2048" to "Meeting at 10:45 in room 2048",
        "Call +91 98765 43210 for support" to "Call +91 98765 43210 for support",
        "Your order 123456 shipped" to "Your order 123456 shipped",
    )

    /** "pass 36/36" or "FAIL: case numbers" (no text, no digits: safe to log). */
    fun selfTest(): String {
        val failed = CASES.withIndex().filter { (_, c) -> extract(c.text, c.trusted) != c.want }.map { it.index + 1 }
        val maskFailed = MASKS.withIndex().filter { (_, m) -> maskCodes(m.first) != m.second }.map { it.index + 1 }
        val total = CASES.size + MASKS.size
        return if (failed.isEmpty() && maskFailed.isEmpty()) "pass $total/$total"
        else "FAIL cases ${failed.joinToString(",")} masks ${maskFailed.joinToString(",")} of $total"
    }
}
