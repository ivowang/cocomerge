from pathlib import Path

from setuptools import find_packages, setup


DESCRIPTION = "Serialized Codex collaboration for shared Git repositories"
CLASSIFIERS = [
    "Development Status :: 3 - Alpha",
    "Environment :: Console",
    "Intended Audience :: Developers",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3 :: Only",
    "Programming Language :: Python :: 3.10",
    "Topic :: Software Development",
    "Topic :: Software Development :: Version Control :: Git",
]
PROJECT_URLS = {
    "Repository": "https://github.com/ivowang/cocomerge",
    "Issues": "https://github.com/ivowang/cocomerge/issues",
}


setup(
    name="cocomerge",
    version="0.1.0",
    description=DESCRIPTION,
    long_description=Path("README.md").read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    author="Cocomerge contributors",
    packages=find_packages("src"),
    package_dir={"": "src"},
    python_requires=">=3.10",
    keywords=["codex", "git", "collaboration", "merge", "worktree"],
    classifiers=CLASSIFIERS,
    project_urls=PROJECT_URLS,
    entry_points={"console_scripts": ["cocomerge=cocomerge.cli:main"]},
)
