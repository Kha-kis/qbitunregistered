import logging
from collections import defaultdict

def tag_cross_seeds(client, torrents):
    """
    Tags torrents as 'cross-seed' if multiple torrents share the same info hash,
    otherwise tags them as 'not-cross-seed'.
    """
    hash_count = defaultdict(list)
    
    # Collect torrents by their info hash
    for torrent in torrents:
        hash_count[torrent.hash].append(torrent)
    
    for hash_value, torrent_list in hash_count.items():
        if len(torrent_list) > 1:
            tag = "cross-seeding"
        else:
            tag = "not-cross-seeding"
        
        # Apply the tag to all torrents with this hash
        for torrent in torrent_list:
            client.torrents_add_tags(torrent_hashes=[torrent.hash], tags=[tag])
            logging.info(f"Added tag '{tag}' to torrent '{torrent.name}'")
    
    logging.info("Cross-seed tagging completed.")

def tag_by_cross_seed(client, torrents, enable_cross_seed_tagging):
    """
    Entry point for tagging torrents based on cross-seed status, based on argument flag.
    """
    if enable_cross_seed_tagging:
        tag_cross_seeds(client, torrents)