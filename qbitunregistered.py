#!/usr/bin/python3
import config
from urllib.parse import urlsplit
from qbittorrentapi import Client

client = Client(host=(config.host), username=(config.username), password=(config.password))
count = 0
unregistered = [
'This torrent does not exist',
'Unregistered torrent',
'002: Invalid InfoHash, Torrent not found',
'Torrent is not authorized for use on this tracker']

for torrent in client.torrents.info():
    for tracker in torrent.trackers:
        for msg in unregistered:
            if msg in tracker.msg:
                count = count + 1
                tracker_short = urlsplit(tracker.url)
                print(torrent.name,' ',tracker.msg,' ',tracker_short.netloc)
                client.torrents_add_tags(tags=(config.tagname),torrent_hashes=(torrent.hash))

print('Torrents Tagged = ', count)
