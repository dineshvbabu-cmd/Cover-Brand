# CoverBrand — vendor concept videos for the container cover

Upload the maker's concept video (or GIF) once, mark the cover panel, import your vendor list,
and the app prints each vendor's **name, logo and contact details onto the moving cover**,
then gives you **one video + one share link per vendor**.

## How it works (5 steps in the web app)
1. **Upload** the concept MP4 / MOV / WEBM / GIF (up to 2 minutes; scaled to max 1920 px wide).
2. **Mark the cover** – drag the 4 orange corners onto the area of the cover where the print should go,
   click *Save corners at this frame*, then *Track cover*.
   - *Auto-track* (default): follows the cover with feature tracking, including perspective as the lid opens/tilts.
   - *Manual*: you save corners on several frames, the app interpolates between them.
   - *Static*: cover doesn't move.
   Scrub the slider: the green dashed box should sit on the cover. Yellow = shaky frames →
   fix the corners on that frame, save, and track again (each saved frame becomes an anchor).
3. **Vendors** – drop any mix of:
   - Excel (.xlsx/.xls) or CSV – headers like *Vendor / Supplier / Company*, *Contact person*, *Email*,
     *Phone / Contact No*, *Website* are detected automatically, title rows above the header are skipped
   - PDF – tables are read; otherwise lines that contain an email/phone
   - Outlook **.msg** or **.eml** emails – attached Excel/PDF lists are read; an email *from* a vendor becomes
     that vendor (company from the signature or email domain, sender as contact) and its attached image is used as the logo
   - Logo images – matched to vendors by file name (`ocean-edge.png` → *Ocean Edge Marine*)
   - or paste lines like `Ocean Edge Marine, Ravi Kumar, ravi@oceanedge.com, +91 98400 12345`
   Every cell is editable; click the logo box to upload/replace a logo.
4. **Print style** – layout, font, colours, optional background plate, lighting realism, margins,
   optional extra line (e.g. "Concept for"). Live preview on any frame.
5. **Render & share** – renders ticked vendors (MP4, plus GIF when the source was a GIF).
   Each vendor gets a public link `/s/<token>` with player + download.
   *Copy all links* or *Download ZIP* (all videos + `share_links.csv` with vendor, email, link).

## Run locally
```bash
pip install -r requirements.txt     # needs ffmpeg on PATH
uvicorn app.main:app --port 8000
# open http://localhost:8000
```
Or `docker build -t coverbrand . && docker run -p 8000:8000 -v $PWD/data:/data coverbrand`

## Deploy on Railway
1. Push this folder to a GitHub repo → *New Project → Deploy from repo* (Dockerfile is picked up).
2. Add a **Volume** mounted at `/data` (projects, logos and videos live there).
3. Variables:
   | Variable | Purpose |
   |---|---|
   | `APP_PASSWORD` | Password for the team UI (browser login prompt; any username). Share links stay public. |
   | `PUBLIC_URL` | Your public domain, e.g. `https://coverbrand.up.railway.app` – used in share links |
   | `RENDER_WORKERS` | Parallel render jobs (default 2; raise with more CPU) |
4. Generate a domain under *Settings → Networking*.

## Good to know
- Rendering is CPU work: a 10 s 1080p clip takes roughly 5–15 s per vendor on a normal instance.
- The print sits on top of the cover. If something passes **in front of** the cover (a hand, crane hook),
  the print will show over it — pick concept clips where the cover stays unobstructed, or use Manual mode.
- Tracking needs some texture on the cover (corrugation, rivets, edges). A perfectly plain, glossy
  CGI panel may track better with Manual mode (2–4 saved frames).
- Share links are unguessable tokens; anyone with the link can view that one vendor's video only.
- Jobs run inside the single web process, so keep `--workers 1`.

## Code map
- `app/main.py` – API, auth, jobs, share pages
- `app/tracking.py` – cover tracking (SIFT homography + optical-flow fallback, keyframe anchors)
- `app/decal.py` – builds the vendor print (logo + name + contact)
- `app/render.py` – perspective warp + lighting blend onto each frame, FFmpeg encoding, GIF output
- `app/vendors.py` – Excel / CSV / PDF / email / text parsing, logo matching
- `app/static/` – web UI and the public share page
