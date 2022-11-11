#!/usr/bin/python3
import config
from urllib.parse import urlsplit
from qbittorrentapi import Client

client = Client(host=(config.host), username=(config.username), password=(config.password))
unregistered = [
'This torrent does not exist',
'Unregistered torrent',
'002: Invalid InfoHash, Torrent not found',
'Torrent is not authorized for use on th tracker',
'Torrent not found',
'Torrent not registered with this tracker.',
'Unregistered torrent',
'unregistered torrent',
'unregistered'
]

for torrent in client.torrents.info():
    for tracker in torrent.trackers:
        if tracker.msg in unregistered and (tracker.status == 4):
                    tracker_short = urlsplit(tracker.url)
                    print(torrent.name,' ',tracker.msg,' ',tracker_short.netloc)
                    client.torrents_add_tags(tags=(config.tagname),torrent_hashes=(torrent.hash))
        elif tracker.msg != 'This torrent is private' and (tracker.status == 4):
                    tracker_short = urlsplit(tracker.url)
                    print(torrent.name,' ',tracker.msg,' ',tracker_short.netloc)
                    client.torrents_add_tags(tags=(tracker.msg),torrent_hashes=(torrent.hash))
