import os

def create_hard_links(target_dir, torrents):
    for torrent in torrents:
        if torrent.state_enum.is_complete:
            source_path = torrent.save_path
            target_path = os.path.join(target_dir, torrent.name)

            try:
                os.link(source_path, target_path)
                print(f"Hard link created for torrent '{torrent.name}'")
            except OSError as e:
                print(f"Failed to create hard link for torrent '{torrent.name}': {str(e)}")
