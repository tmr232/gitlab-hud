from setuptools import find_packages, setup


def get_requirements():
    with open("requirements.txt") as f:
        return f.read().splitlines()


setup(
    name="gitlab-hud",
    version="1",
    packages=find_packages(where="src"),
    url="",
    license="",
    author="Tamir Bahar",
    author_email="",
    description="",
    package_dir={"": "src"},
    install_requires=get_requirements(),
    entry_points={
        "console_scripts": ["ghud=gitlab_hud.hud:entry_point"],
    },
)
