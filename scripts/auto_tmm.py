from qbittorrentapi import Client
import logging

def apply_auto_tmm_per_torrent(client, torrents):
    for torrent in torrents:
        client.torrents_set_auto_management(
            enable=True,
            torrent_hashes=[torrent.hash]
        )
        logging.info(f"Enabled auto TMM for torrent with name '{torrent.name}'")
