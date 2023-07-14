import os

def create_hard_links(target_dir, torrents):
    for torrent in torrents:
        if torrent.state_enum.is_completed:
            content_path = os.path.join(torrent.save_path, torrent.name)

            if os.path.isdir(content_path):
                for root, dirs, files in os.walk(content_path):
                    for file in files:
                        source_path = os.path.join(root, file)
                        target_path = os.path.join(target_dir, torrent.category or '', file)

                        try:
                            os.makedirs(os.path.dirname(target_path), exist_ok=True)
                            os.link(source_path, target_path)
                            print(f"Hard link created for file '{source_path}'")
                        except OSError as e:
                            print(f"Failed to create hard link for file '{source_path}': {str(e)}")
            else:
                print(f"Skipping non-directory torrent '{torrent.name}'")
