"""Upload the backend to a Hugging Face Docker Space.

Usage (from anywhere): python deploy/huggingface/deploy.py <hf-user>/<space-name>
       python deploy/huggingface/deploy.py --dry-run [--stage-dir DIR]

WARNING: the Space is created PUBLIC, and ALL uploaded files are public: the
backend code, the vendored wildcat and assets/asbpa_data. Check you are allowed
to publish them first. A private Space can't be called from the website.

Needs `pip install -U huggingface_hub` and `hf auth login` (write token); not
needed with --dry-run. Only files committed to git (HEAD) are uploaded, so
.env, logs and local run folders can never be included.
"""
import argparse
import io
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PATHS = ["Dockerfile", "assets/backend", "assets/wildcat", "assets/asbpa_data"]
# Large files listed in .dockerignore; excluded even if someone commits them.
SKIP = {
    "assets/asbpa_data/a7_vertical_land_motion/California_LLVS.txt",
    "assets/asbpa_data/a8_post_wildfire_debris_flow/combined_wildfire_debris_flow.shp",
    "assets/asbpa_data/a4_annual_high_tide_flood_days/processed_shoreline_flood_days.geojson",
}


def git(*args):
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True
    ).stdout


def stage(dest):
    dirty = git("status", "--porcelain", "--", *PATHS).decode()
    if dirty:
        print("WARNING: uncommitted changes (NOT uploaded; commit them first):")
        print(dirty)
    data = git("archive", "HEAD", *PATHS)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        members = [m for m in tar.getmembers() if m.name not in SKIP]
        tar.extractall(dest, members=members)
    card = ROOT / "deploy" / "huggingface" / "README.md"
    (Path(dest) / "README.md").write_bytes(card.read_bytes())


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("space", nargs="?", help="<hf-user>/<space-name>")
    p.add_argument("--dry-run", action="store_true", help="stage only, no upload")
    p.add_argument("--stage-dir", help="stage here (must be empty); kept afterwards")
    a = p.parse_args()
    if not a.space and not a.dry_run:
        p.error("space is required unless --dry-run")
    print(__doc__.split("WARNING: ")[1].split("\n\n")[0].replace("\n", " "), "\n")

    with tempfile.TemporaryDirectory() as tmp:
        dest = a.stage_dir or tmp
        stage(dest)
        files = [f for f in Path(dest).rglob("*") if f.is_file()]
        mb = sum(f.stat().st_size for f in files) / 1e6
        print(f"Staged {len(files)} files, {mb:.1f} MB in {dest}")
        if a.dry_run:
            return
        from huggingface_hub import create_repo, upload_folder

        create_repo(
            a.space, repo_type="space", space_sdk="docker", exist_ok=True, private=False
        )
        # delete_patterns only removes remote files under assets/ that are not in
        # this upload; the Dockerfile and Space card are outside it.
        upload_folder(
            repo_id=a.space,
            repo_type="space",
            folder_path=dest,
            delete_patterns=["assets/**"],
        )
        print(f"Done: https://huggingface.co/spaces/{a.space}")


if __name__ == "__main__":
    sys.exit(main())
