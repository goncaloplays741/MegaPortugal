package com.megaportugal.app

import okhttp3.OkHttpClient
import okhttp3.Request
import java.util.concurrent.TimeUnit

object M3ULoader {
    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build()

    private val LISTAS = listOf(
        "https://oneplayhd.com/listas_oneplay/lista02.txt",
        "https://oneplayhd.com/listas_oneplay/lista05.txt"
    )

    fun fetchChannels(): List<Channel> {
        val all = mutableListOf<Channel>()
        for (url in LISTAS) {
            try {
                val text = downloadText(url)
                all.addAll(parseM3U(text))
            } catch (_: Exception) { }
        }
        return all.distinctBy { it.url }.sortedBy { it.name.lowercase() }
    }

    private fun downloadText(url: String): String {
        val req = Request.Builder().url(url).build()
        val res = client.newCall(req).execute()
        if (!res.isSuccessful) throw Exception("HTTP ${res.code}")
        return res.body?.string() ?: throw Exception("Empty body")
    }

    private fun parseM3U(text: String): List<Channel> {
        val channels = mutableListOf<Channel>()
        var currentName = ""
        var currentLogo = ""
        var currentTvgId = ""
        for (line in text.lines()) {
            val trimmed = line.trim()
            if (trimmed.startsWith("#EXTINF:")) {
                currentLogo = extractAttr(trimmed, "tvg-logo")
                currentTvgId = extractAttr(trimmed, "tvg-id")
                val idx = trimmed.lastIndexOf(',')
                currentName = if (idx >= 0) trimmed.substring(idx + 1).trim() else "Canal"
            } else if (trimmed.startsWith("http://") || trimmed.startsWith("https://")) {
                if (currentName.isNotEmpty()) {
                    channels.add(Channel(currentName, trimmed, currentLogo, currentTvgId))
                }
                currentName = ""
                currentLogo = ""
                currentTvgId = ""
            }
        }
        return channels
    }

    private fun extractAttr(line: String, attr: String): String {
        val regex = Regex("""$attr="([^"]*)"""", RegexOption.IGNORE_CASE)
        return regex.find(line)?.groupValues?.getOrElse(1) { "" } ?: ""
    }
}
