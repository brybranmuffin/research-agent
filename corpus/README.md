# Spinosaurus ecology corpus

A fixed, reproducible source corpus for the research question:

> **Was *Spinosaurus aegyptiacus* an aquatic pursuit predator, and how strong is the evidence?**

It covers the aquatic/diving hypothesis, the shoreline/wading ("heron-like") hypothesis, neutral background (anatomy, discovery history, environment), and a few tangential spinosaurid papers.

## Fetching

```bash
cd corpus
python fetch_corpus.py            # downloads into raw/, verifies sha256; safe to re-run
python fetch_corpus.py --only sereno2022_elife,henderson2018_peerj
python fetch_corpus.py --force    # re-download everything and re-verify
```

- Requires Python 3.8+ and `requests` (nothing else).
- Downloads the 34 entries with `status: "ok"` (~125 MB), with a 1 s pause between requests and backoff on 429/5xx. A cold run takes a few minutes; bioRxiv usually rate-limits once.
- A re-run makes no network calls: files whose hash already matches are skipped.
- Exit code is non-zero if any entry fails to download or verify. On a hash mismatch the new bytes are saved as `raw/{id}.{type}.mismatch` and the verified file is left untouched.
- Hard cap: any document over 100 MB is rejected.
- `--update-manifest` records `sha256`/`bytes`/`retrieved_at` for entries whose hash is still `null`. Use it only when adding new entries.

`raw/` is gitignored, so the repository holds only metadata and the script. Each user downloads the documents from the original sources.

## Layout

```
corpus/
  manifest.json     # 40 entries: the contract
  fetch_corpus.py
  raw/              # {id}.pdf / {id}.html  (gitignored)
  README.md
```

### Manifest conventions

- `status: "unavailable"` (6 entries): no file is downloaded and `url`, `sha256`, `bytes` and `retrieved_at` are `null`. These are key papers with no legal free copy, kept so agents can recognise citations to them. `notes` names the corpus documents that summarise each one.
- `license: "fetch-only"` (8 entries): a legal free copy exists, but its terms grant no reuse licence. The files are downloaded for **private, non-commercial research only and must not be redistributed**. See the licensing section below.
- PDF URLs are version-pinned where the host allows it: bioRxiv `v1`, eLife `-v1.pdf`, OSF `?version=1`, and PMC Open Access dataset keys `PMCxxxx.N`.
- Wikipedia pages are pinned to a revision ID (`/api/rest_v1/page/html/{title}/{revid}`). News pages are pinned to Wayback Machine captures (`…/web/{timestamp}id_/…`, which returns the original bytes) where a capture exists.
- `landing_url` is the human-readable page: the DOI, article page, or Wikipedia `oldid` link.

## Sources and licensing

| Licence | Entries | Notes |
|---|---|---|
| CC-BY-4.0 | 17 | eLife, PeerJ, PLOS ONE, Sci. Reports, Life, Geol. Mag., APP, ACS Cent. Sci., PaleorXiv. One of these (lakin2019) is unavailable because the host blocks scripted download. |
| CC-BY-SA-4.0 | 7 | English Wikipedia revisions |
| CC-BY-NC-SA-4.0 | 2 | Palaeontologia Electronica |
| CC-BY-NC-ND-4.0 | 1 | bioRxiv preprint (Fabbri et al. reply) |
| fetch-only | 8 | 1 author manuscript ("All Rights Reserved"), 1 HAL deposit, 1 bioRxiv "no reuse" preprint, 5 news/press pages |
| all-rights-reserved | 5 | Closed papers recorded as unavailable |

The NC licences and the fetch-only items are compatible with this project only because it is non-commercial research. Re-check them before any commercial use or redistribution.

**Download routes.** PeerJ, MDPI, Europe PMC and ScienceDirect block scripted downloads (Cloudflare 403). Papers deposited in PubMed Central are therefore fetched from the [PMC Open Access dataset on AWS](https://registry.opendata.aws/ncbi-pmc/) (`pmc-oa-opendata`), which is public, version-pinned and records the licence of each article. Their `landing_url` still points to the publisher.

**Known fragilities**
- bioRxiv returns HTTP 429 to scripted clients. The fetcher backs off for 10, 20, 40… seconds and normally succeeds on the first retry.
- Wikimedia may re-render old revisions after parser upgrades. A future sha256 mismatch on a `wiki_*` file then reflects re-rendering, not an edit.
- `harvard2020_gazette` and `uchicago2022_news` have no Wayback capture and are fetched live. They were byte-stable on 2026-10-01 but will break if those sites change.

## Counts

**By type:** 40 entries = 28 PDF (22 downloaded + 6 unavailable) + 12 HTML (all downloaded). That gives **34 files on disk**.

**By stance** (from evaluation labels kept outside `corpus/`):

| Stance | All entries | On disk | On disk, openly licensed only |
|---|---|---|---|
| aquatic | 13 | 8 | 5 |
| wading | 7 | 6 | 4 |
| mixed | 7 | 7 | 4 |
| background | 10 | 10 | 10 |
| tangential | 3 | 3 | 3 |

On disk, the aquatic and wading sides are roughly even (8 vs 6). Most of the aquatic entries that are not on disk are closed-access primary papers (Ibrahim 2014, Fabbri 2022, Amiot 2010, Beevor 2021), so an agent sees their arguments mainly through replies, summaries and press coverage.

## Unavailable papers and their stand-ins

| id | Why | Covered by |
|---|---|---|
| ibrahim2014_science | Closed (Science) | honeholtz2021_pe, henderson2018_peerj, sereno2022_elife, wiki_spinosaurus |
| fabbri2022_nature | Closed (Nature) | fabbri2022_biorxiv_reply, myhrvold2022_biorxiv, myhrvold2024_plosone |
| amiot2010_geology | Closed; HAL record has no file | hassler2018_procb, acs2026_isotopes |
| beevor2021_cretres | Closed (Cretaceous Research) | sereno2022_elife, honeholtz2021_pe |
| lakin2019_cretres | CC-BY, but ScienceDirect blocks scripted download | Download manually from `landing_url` if needed |
| sereno2026_science | Closed (Science) | wiki_spinosaurus_mirabilis |

## Deliberately excluded

- Over the 100 MB size policy: Ibrahim et al. 2020 *ZooKeys* Kem Kem monograph (119 MB), Evers et al. 2015 *PeerJ* on *Sigilmassasaurus* (106 MB), Barker et al. 2022 *PeerJ* "A European giant" (115 MB), Schade et al. 2023 *Palaeontologia Electronica* on *Irritator* (346 MB).
- Duplicates: the bioRxiv preprint of Sereno et al. 2022 and the 2023 bioRxiv version of Myhrvold et al. ("no reuse"; the PLOS ONE version is in the corpus).
- Unclear licence: Hone & Holtz 2019, a comment in *Cretaceous Research* (UMD repository copy with no stated licence).
