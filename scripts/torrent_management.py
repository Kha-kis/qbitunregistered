import logging
from qbittorrentapi import Client

def pause_torrents(client, torrents):
    total_paused = 0
    for torrent in torrents:
        client.torrents_pause(torrent.hash)
        total_paused += 1

    logging.info("Paused %d torrents.", total_paused)

def resume_torrents(client, torrents):
    total_resumed = 0
    for torrent in torrents:
        client.torrents_resume(torrent.hash)
        total_resumed += 1

    logging.info("Resumed %d torrents.", total_resumed)
