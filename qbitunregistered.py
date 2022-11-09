#!/usr/bin/python3
import config
from qbittorrentapi import Client
client = Client(host=(config.host), username=(config.username), password=(config.password))
count = 0

for torrent in client.torrents.info():
    for status in torrent.trackers:
        if ('This torrent does not exist' or 'Unregistered torrent' or '002: Invalid InfoHash, Torrent not found' or 'Torrent is not authorized for use on this tracker') in status.msg:
            a = torrent.tracker.split('/')
            count = count + 1
            print(torrent.name,' ',status.msg,' ',a[2])
            torrent.delete(hash=(torrent.hash),delete_files=(config.delete_files))

print('Torrents Deleted = ', ' ', count)
