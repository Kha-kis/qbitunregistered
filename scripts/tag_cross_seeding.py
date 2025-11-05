import logging
from collections import defaultdict

def tag_cross_seeds(client, torrents):
    """
    Tags torrents as 'cross-seed' if multiple torrents share the same file structure,
    otherwise tags them as 'not-cross-seeding'.
    """
    path_count = defaultdict(list)
    file_structure_map = defaultdict(list)
    tag_counts = {"cross-seed": 0, "not-cross-seeding": 0}

    logging.info(f"Total torrents fetched: {len(torrents)}")

    # Collect torrents by their save path and file list
    for torrent in torrents:
        torrent_path = getattr(torrent, 'save_path', None)
        torrent_files = client.torrents_files(torrent.hash)

        if not torrent_path or not torrent_files:
            logging.warning(f"Skipping torrent '{torrent.name}' due to missing save path or file list.")
            continue

        file_structure = frozenset(f["name"] for f in torrent_files)
        file_structure_map[file_structure].append(torrent)

    logging.info(f"Collected {len(file_structure_map)} unique file structures from torrents.")

    # Debug: Print file structure occurrences
    for file_structure, torrent_list in file_structure_map.items():
        logging.debug(f"File structure {file_structure} found in {len(torrent_list)} torrents.")

    # Apply tags
    for file_structure, torrent_list in file_structure_map.items():
        if len(torrent_list) > 1:
            tag = "cross-seed"
        else:
            tag = "not-cross-seeding"

        tag_counts[tag] += len(torrent_list)

        # Apply the tag to all torrents with this file structure
        for torrent in torrent_list:
            client.torrents_add_tags(torrent_hashes=[torrent.hash], tags=[tag])
            logging.info(f"Added tag '{tag}' to torrent '{torrent.name}'")

    logging.info(f"Cross-seed tagging completed: {tag_counts['cross-seed']} cross-seed torrents, {tag_counts['not-cross-seeding']} not-cross-seeding torrents.")