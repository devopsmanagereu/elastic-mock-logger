"""Build and publish versioned Docker images for this project."""

import argparse
from pathlib import Path
import subprocess
import sys
import tomllib


PROJECT_DIRECTORY = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = PROJECT_DIRECTORY / "pyproject.toml"
DOCKERFILE_PATH = PROJECT_DIRECTORY / "build_src" / "Dockerfile"
PLATFORM = "linux/amd64"


def get_image_settings() -> tuple[str, str, str]:
    """Read the image repository, project name, and version from pyproject.toml."""
    with PYPROJECT_PATH.open("rb") as pyproject_file:
        pyproject = tomllib.load(pyproject_file)
    project = pyproject["project"]
    image_repository = pyproject["tool"]["elastic-mock-logger"]["image_repository"]
    return image_repository, project["name"], project["version"]


def image_exists(image: str) -> bool:
    """Return whether Docker already has an image with this exact tag."""
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def build_image(versioned_image: str, latest_image: str) -> int:
    """Build and tag an image unless its version already exists locally."""
    if image_exists(versioned_image):
        print(f"Image {versioned_image} already exists.")
        print("Increase project.version in pyproject.toml before building.")
        return 1

    print(f"Building {versioned_image} for {PLATFORM}.")
    subprocess.run(
        [
            "docker",
            "buildx",
            "build",
            "--platform",
            PLATFORM,
            "--file",
            str(DOCKERFILE_PATH),
            "--tag",
            versioned_image,
            "--load",
            str(PROJECT_DIRECTORY),
        ],
        check=True,
    )
    subprocess.run(
        ["docker", "tag", versioned_image, latest_image],
        check=True,
    )
    print(f"Built {versioned_image} and tagged it as {latest_image}.")
    return 0


def push_images(versioned_image: str, latest_image: str) -> int:
    """Push the versioned image and its latest tag to the configured registry."""
    missing_images = [
        image for image in (versioned_image, latest_image) if not image_exists(image)
    ]
    if missing_images:
        print("The following images are not available locally:")
        for image in missing_images:
            print(f"- {image}")
        print("Run the build command before pushing.")
        return 1

    for image in (versioned_image, latest_image):
        print(f"Pushing {image}.")
        subprocess.run(["docker", "push", image], check=True)
    return 0


def parse_arguments() -> argparse.Namespace:
    """Parse the requested image-management command."""
    parser = argparse.ArgumentParser(
        description="Build or push the versioned Docker image for this project."
    )
    parser.add_argument("command", choices=("build", "push"))
    return parser.parse_args()


def main() -> int:
    """Execute the requested Docker image-management command."""
    arguments = parse_arguments()
    image_repository, image_name, version = get_image_settings()
    versioned_image = f"{image_repository}/{image_name}:{version}"
    latest_image = f"{image_repository}/{image_name}:latest"

    if arguments.command == "build":
        return build_image(versioned_image, latest_image)
    return push_images(versioned_image, latest_image)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FileNotFoundError:
        print("Docker is required to manage Docker images.", file=sys.stderr)
        raise SystemExit(1) from None
