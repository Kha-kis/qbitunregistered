import os

def check_files_on_disk(client):
    # Retrieve the save paths for each category
    categories = client.torrents_categories()

    if not isinstance(categories, list):
        # Handle case where only a single category is returned as a string
        categories = [categories]

    # Filter out categories without savePath
    category_save_paths = [category["savePath"] for category in categories if category.get("savePath")]

    # Iterate over the save paths and check for orphaned files
    orphaned_files = []
    for save_path in category_save_paths:
        files_on_disk = os.listdir(save_path)
        torrents = client.torrents.info()
        for file in files_on_disk:
            if is_orphaned(file, torrents):
                orphaned_files.append(os.path.join(save_path, file))

    # Return the list of orphaned files
    return orphaned_files

def is_orphaned(file, torrents):
    # Check if the file is orphaned based on the list of torrents
    for torrent in torrents:
        if torrent.content_path == file:
            # File is associated with a torrent, not orphaned
            return False
    
    # File is orphaned
    return True

def process_orphaned_file(save_path, file):
    # Perform actions for orphaned file
    # For example, you can print the file path
    print(f"Orphaned file: {os.path.join(save_path, file)}")
