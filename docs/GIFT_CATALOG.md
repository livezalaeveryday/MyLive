# TikTok gift images

Gift images now come only from `giftImageUrl` on a TikTok gift event. The connector selects the first valid URL from `gift.image.url_list`, then `gift.icon.url_list`. Only HTTPS URLs whose host is `tiktokcdn.com` or a subdomain are accepted, with no credentials or explicit port and at most 2048 characters. Invalid URLs are discarded independently of the gift text.

Dashboard feed, chat overlay and highlight use the same renderer. Missing URL, rejected URL or image loading error displays 🎁 while keeping gift name, count, value and author. YouTube rendering is unchanged. The app does not download, save or cache gift media; the browser loads the CDN image directly, so internet/CDN availability is required and normal browser caching may apply. No combo/count semantics have changed.

`Object/gifts_773.zip`, all 773 PNGs and `server/static/gifts/catalog.json` are preserved as an inactive archive. Rendering never fetches this catalog and does not match local images by name or ID. The importer remains an archival utility; it is not invoked automatically. Historical reports remain unchanged.

Run `python -m unittest discover -s tests -p test_gift_image_url.py` and `node tests/test_gift_renderer.cjs`. `python scripts/gift_fixture.py` shows Rose with its event CDN URL and White Dove without an image even though its local archive asset exists. Fixture QA is not real LIVE/combo verification.
