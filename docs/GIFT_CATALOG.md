# Local TikTok gift images

`server/static/gifts/catalog.json` is user editable. 773 PNGs are imported from
`Object/gifts_773.zip`; the ZIP is preserved. File numbers are inventory sequence
numbers, **not TikTok gift IDs**. The source ZIP has no authoritative ID table.

Each entry has `asset`, `names`, `verifiedIds`, and `sha256`. Names inferred from
filename suffixes are only candidates; blank names stay unmapped. Add exact live
gift names (including Thai aliases) to `names`. Unicode NFKC, case, whitespace
normalization are supported; matching is exact and must identify one entry.
Duplicates produce a generic gift icon. No fuzzy matching or translation occurs.

Put IDs confirmed against an actual TikTok gift event in `verifiedIds` as decimal
strings, e.g. `"123456"`. Do not copy sequence numbers into this field. A unique
verified ID takes precedence over names. Duplicate ID assignments fail closed.
Unknown IDs may still use a unique exact name. YouTube gifts never use this TikTok
catalog. Event-supplied `giftImage` URLs are ignored.

Edit the catalog beside the app's deployed static assets and reload browser/OBS
sources. For a one-file EXE, bundled files are temporary runtime assets: edit the
source catalog and rebuild for persistence. Missing catalog or broken images
fall back to 🎁 while preserving author, gift name/count/value and highlighting.

`python scripts/import_gifts.py` imports the archive once; it rejects traversal,
duplicate/oversized files, invalid PNG signatures and overwrites. It refuses to
replace an existing catalog so user mappings remain intact. This verifies PNG
signatures, not a full image decode. No live gift receipt or combo behavior was
changed; repeat counts are displayed as supplied by the existing connector.
The installed TikTokLive event exposes `gift_id`; the adapter uses it first and
falls back to `gift.id` when absent. Mapping accuracy still requires real LIVE
validation. All 773 imported PNGs passed Pillow structural verification.
