import click
import tarfile
from odc.azure import download_yamls, find_blobs
from odc.io.tar import add_txt_file, tar_mode


@click.command("azure-to-tar")
@click.argument("account_url", type=str, nargs=1)
@click.argument("container_name", type=str, nargs=1)
@click.argument("credential", type=str, nargs=1)
@click.argument("prefix", type=str, nargs=1)
@click.argument("suffix", type=str, nargs=1)
@click.option(
    "--workers", "-w", type=int, default=32, help="Number of threads to download blobs"
)
@click.option(
    "--outfile", type=str, default="metadata.tar.gz", help="Sets the output file name"
)
def cli(
    account_url: str,
    container_name: str,
    credential: str,
    prefix: str,
    suffix: str,
    workers: int,
    outfile: str,
):
    print(f"Opening AZ Container {container_name} on {account_url}")
    print(f"Searching on prefix '{prefix}' for files matching suffix '{suffix}'")
    yaml_urls = find_blobs(container_name, credential, prefix, suffix, account_url)

    print(f"Found {len(yaml_urls)} datasets")
    yamls = download_yamls(account_url, container_name, credential, yaml_urls, workers)

    url_prefix = (account_url + "/" + container_name + "/")[len("https://") :]

    # jam it all in a tar
    tar_opts = {
        "name": outfile,
        "mode": "w" + tar_mode(gzip=True, xz=True, is_pipe=False),
    }
    with tarfile.open(**tar_opts) as tar:
        for yaml in yamls:
            add_txt_file(tar=tar, content=yaml[0], fname=url_prefix + yaml[1])

    print("Done!")


if __name__ == "__main__":
    cli()
