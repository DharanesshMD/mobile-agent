package ai.mobileagent.client

import android.os.Build
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/** Exchanges the one-time code shown in the web app for a device token (POST /api/pair). */
object Pairing {
    private val http = OkHttpClient.Builder().callTimeout(15, TimeUnit.SECONDS).build()

    /** Returns the token, or throws with a message the owner can read. */
    fun pair(server: String, code: String): String {
        val body = JSONObject().put("code", code.trim()).put("name", Build.MODEL ?: "Android phone").toString()
            .toRequestBody("application/json".toMediaType())
        val req = Request.Builder().url(server.trim().trimEnd('/') + "/api/pair").post(body).build()
        http.newCall(req).execute().use { res ->
            val text = res.body?.string().orEmpty()
            if (!res.isSuccessful) {
                val detail = runCatching { JSONObject(text).optString("detail") }.getOrDefault("")
                throw IllegalStateException(detail.ifBlank { "the server answered ${res.code}" })
            }
            return JSONObject(text).getString("token")
        }
    }
}
