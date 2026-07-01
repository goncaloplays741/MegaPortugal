package com.megaportugal.app

import android.content.Intent
import android.os.AsyncTask
import android.os.Bundle
import android.view.View
import android.widget.ProgressBar
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView

class MainActivity : AppCompatActivity() {
    private lateinit var recycler: RecyclerView
    private lateinit var progressBar: ProgressBar
    private lateinit var statusText: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        recycler = findViewById(R.id.channel_list)
        progressBar = findViewById(R.id.progress)
        statusText = findViewById(R.id.status)
        loadChannels()
    }

    private fun loadChannels() {
        progressBar.visibility = View.VISIBLE
        object : AsyncTask<Void, Void, List<Channel>>() {
            override fun doInBackground(vararg params: Void?): List<Channel> {
                return M3ULoader.fetchChannels()
            }

            override fun onPostExecute(channels: List<Channel>) {
                progressBar.visibility = View.GONE
                if (channels.isEmpty()) {
                    statusText.visibility = View.VISIBLE
                    statusText.text = "Erro ao carregar canais.\nVerifica a tua internet."
                    return
                }
                showChannels(channels)
            }
        }.execute()
    }

    private fun showChannels(channels: List<Channel>) {
        recycler.visibility = View.VISIBLE
        recycler.layoutManager = LinearLayoutManager(this)
        recycler.adapter = ChannelAdapter(channels) { ch ->
            val intent = Intent(this, PlayerActivity::class.java).apply {
                putExtra("name", ch.name)
                putExtra("url", ch.url)
            }
            startActivity(intent)
        }
    }
}
