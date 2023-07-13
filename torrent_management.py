import logging
from qbittorrentapi import Client

def pause_torrents(client, torrents):
    for torrent in torrents:
        client.torrents_pause(torrent.hash)
        logging.info(f"Paused torrent with name '{torrent.name}'")

def resume_torrents(client, torrents):
    for torrent in torrents:
        client.torrents_resume(torrent.hash)
        logging.info(f"Resumed torrent with name '{torrent.name}'")