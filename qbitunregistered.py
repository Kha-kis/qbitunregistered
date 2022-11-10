#!/usr/bin/python3
import config
from urllib.parse import urlsplit
from qbittorrentapi import Client
client = Client(host=(config.host), username=(config.username), password=(config.password))
count = 0

for torrent in client.torrents.info():
    for status in torrent.trackers:
        if ('This torrent does not exist' or 'Unregistered torrent' or '002: Invalid InfoHash, Torrent not found' or 'Torrent is not authorized for use on this tracker') in status.msg:
            count = count + 1
            tracker = urlsplit(torrent.tracker)
            print(torrent.name,' ',status.msg,' ',tracker.netloc)
            client.torrents_add_tags(tags=(config.tagname).torrent_hashes=(torrent.hash))

print('Torrents Tagged = ', ' ', count)
