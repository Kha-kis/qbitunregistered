import logging
import datetime

def tag_by_age(client, torrents, config):
    current_time = datetime.datetime.now()

    for torrent in torrents:
        # Calculate the age of the torrent in months
        torrent_age_months = (current_time.year - torrent.creation_date.year) * 12 + (current_time.month - torrent.creation_date.month)

        # Determine the appropriate tag based on age buckets in months
        if torrent_age_months <= 1:
            tag = '>1_month'
        elif torrent_age_months <= 2:
            tag = '>2_months'
        elif torrent_age_months <= 3:
            tag = '>3_months'
        elif torrent_age_months <= 4:
            tag = '>4_months'
        elif torrent_age_months <= 5:
            tag = '>5_months'
        elif torrent_age_months <= 6:
            tag = '>6_months'
        else:
            tag = '6_months_plus'

        # Add the tag to the torrent
        client.torrents_add_tags(torrent_hashes=[torrent.hash], tags=[tag])
        logging.info(f"Added tag '{tag}' to torrent with name '{torrent.name}'")

    logging.info("Tagging by age buckets in months completed.")
