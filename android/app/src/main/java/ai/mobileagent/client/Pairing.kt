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

    private fun base(server: String) = server.trim().trimEnd('/')

    /** The phone bot's recent tasks (GET /api/me/history with the device token). */
    fun history(server: String, token: String): org.json.JSONArray {
        val req = Request.Builder().url(base(server) + "/api/me/history").header("Authorization", "Bearer $token").build()
        http.newCall(req).execute().use { res ->
            val text = res.body?.string().orEmpty()
            if (!res.isSuccessful) throw IllegalStateException(if (res.code == 401) "this phone is no longer paired" else "the server answered ${res.code}")
            return org.json.JSONArray(text)
        }
    }

    /** Tell the server to forget this phone's token (POST /api/me/revoke). Returns normally when the server did, or when it was already gone. */
    fun revoke(server: String, token: String) {
        val req = Request.Builder().url(base(server) + "/api/me/revoke").header("Authorization", "Bearer $token")
            .post("".toRequestBody("application/json".toMediaType())).build()
        http.newCall(req).execute().use { res ->
            if (!res.isSuccessful && res.code != 401) throw IllegalStateException("the server answered ${res.code}")
        }
    }
}
