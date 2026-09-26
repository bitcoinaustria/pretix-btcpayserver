from setuptools import find_namespace_packages, setup

setup(
    name="pretix-btcpayserver",
    version="0.2.0",
    description="Accept Bitcoin payments in pretix via the BTCPay Server Greenfield API",
    long_description="""# pretix BTCPay Server

Accept Bitcoin Lightning and on-chain Bitcoin payments in [pretix](https://pretix.eu/) using the [BTCPay Server](https://btcpayserver.org/) **Greenfield API** (not the deprecated BitPay-compatible API).

The customer pays on the BTCPay checkout page hosted by your BTCPay Server instance. A signed store webhook and a periodic poll keep pretix in sync with the invoice state, which is always read back from the API.
""",
    long_description_content_type="text/markdown",
    author="Nathan Day",
    author_email="nathan@day.ag",
    url="https://github.com/bitcoinaustria/pretix-btcpayserver",
    license="MIT",
    install_requires=["django>=4", "i18nfield>=0.6", "requests>=2"],
    packages=find_namespace_packages(include=["pretix_btcpay", "pretix_btcpay.*"]),
    package_data={
        "pretix_btcpay": ["templates/pretix_btcpay/*.html", "locale/*/LC_MESSAGES/*"],
    },
    include_package_data=True,
    entry_points="""
[pretix.plugin]
pretix_btcpay=pretix_btcpay:PretixPluginMeta
""",
    python_requires=">=3.11",
)