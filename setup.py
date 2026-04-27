from setuptools import find_packages, setup


setup(
    name="cocomerge",
    version="0.1.0",
    description="Serialized Codex collaboration for shared Git repositories",
    packages=find_packages("src"),
    package_dir={"": "src"},
    python_requires=">=3.10",
    entry_points={"console_scripts": ["cocomerge=cocomerge.cli:main"]},
)
