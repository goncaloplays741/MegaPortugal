package com.megaportugal.app

import android.net.Uri
import android.os.Bundle
import android.view.View
import android.widget.ProgressBar
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.media3.common.MediaItem
import androidx.media3.common.Player
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.ui.PlayerView

class PlayerActivity : AppCompatActivity() {
    private var player: ExoPlayer? = null
    private lateinit var playerView: PlayerView
    private lateinit var bufferProgress: ProgressBar

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_player)

        val name = intent.getStringExtra("name") ?: "Canal"
        val url = intent.getStringExtra("url")

        supportActionBar?.title = name
        supportActionBar?.setDisplayHomeAsUpEnabled(true)

        playerView = findViewById(R.id.player_view)
        bufferProgress = findViewById(R.id.buffer_progress)

        if (url == null) {
            Toast.makeText(this, "URL inválida", Toast.LENGTH_SHORT).show()
            finish()
            return
        }

        player = ExoPlayer.Builder(this).build().apply {
            setMediaItem(MediaItem.fromUri(Uri.parse(url)))
            prepare()
            playWhenReady = true
            addListener(object : Player.Listener {
                override fun onPlaybackStateChanged(state: Int) {
                    bufferProgress.visibility =
                        if (state == Player.STATE_BUFFERING) View.VISIBLE else View.GONE
                }
                override fun onPlayerError(error: androidx.media3.common.PlaybackException) {
                    Toast.makeText(this@PlayerActivity,
                        "Erro: ${error.localizedMessage}", Toast.LENGTH_LONG).show()
                }
            })
        }
        playerView.player = player
    }

    override fun onSupportNavigateUp(): Boolean {
        finish()
        return true
    }

    override fun onStop() {
        super.onStop()
        player?.pause()
    }

    override fun onDestroy() {
        super.onDestroy()
        player?.release()
        player = null
    }
}
