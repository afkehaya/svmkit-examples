from typing import Union

import pulumi
import pulumi_aws as aws
import pulumi_tls as tls
import pulumi_svmkit as svmkit

from .network import external_sg, internal_sg

ami = aws.ec2.get_ami(
    filters=[
        {
            "name": "name",
            "values": ["debian-12-*"],
        },
        {
            "name": "architecture",
            "values": ["x86_64"],
        },
    ],
    owners=["136693071363"],  # Debian
    most_recent=True,
).id

agave_version = "1.18.24-1"

class Node:
    def __init__(self, name):
        self.name = name

        def _(s):
            return f"{self.name}-{s}"

        self.ssh_key = tls.PrivateKey(_("ssh-key"), algorithm="ED25519")
        self.key_pair = aws.ec2.KeyPair(
            _("keypair"), public_key=self.ssh_key.public_key_openssh)

        self.validator_key = svmkit.KeyPair(_("validator-key"))
        self.vote_account_key = svmkit.KeyPair(_("vote-account-key"))

        self.instance = aws.ec2.Instance(
            _("instance"),
            ami=ami,
            instance_type="m5.2xlarge",
            key_name=self.key_pair.key_name,
            vpc_security_group_ids=[external_sg.id, internal_sg.id],
            ebs_block_devices=[
                {
                    "device_name": "/dev/sdf",
                    "volume_size": 500,
                    "volume_type": "io2",
                    "iops": 5000,
                },
                {
                    "device_name": "/dev/sdg",
                    "volume_size": 1024,
                    "volume_type": "io2",
                    "iops": 5000,
                },
            ],
            user_data="""#!/bin/bash
mkfs -t ext4 /dev/sdf
mkfs -t ext4 /dev/sdg
mkdir -p /home/sol/accounts
mkdir -p /home/sol/ledger
cat <<EOF >> /etc/fstab
/dev/sdf	/home/sol/accounts	ext4	defaults	0	0
/dev/sdg	/home/sol/ledger	ext4	defaults	0	0
EOF
systemctl daemon-reload
mount -a
"""
        )

        self.connection = svmkit.ssh.ConnectionArgsDict({
            "host": self.instance.public_dns,
            "user": "admin",
            "private_key": self.ssh_key.private_key_openssh
        })

    def configure_validator(self, flags: Union['svmkit.agave.FlagsArgs', 'svmkit.agave.FlagsArgsDict'], depends_on=[]):
        return svmkit.validator.Agave(
            f"{self.name}-validator",
            connection=self.connection,
            version=agave_version,
            key_pairs={
                "identity": self.validator_key.json,
                "vote_account": self.vote_account_key.json,
            },
            flags=flags,
            opts=pulumi.ResourceOptions(
                depends_on=([self.instance] + depends_on))
        )


class Genesis:
    def __init__(self, bootstrap_node: Node):
        self.bootstrap_node = bootstrap_node
        self.faucet_key = svmkit.KeyPair("faucet-key")
        self.treasury_key = svmkit.KeyPair("treasury-key")
        self.stake_account_key = svmkit.KeyPair("stake-account-key")

        self.genesis = svmkit.genesis.Solana(
            "genesis",
            connection=self.bootstrap_node.connection,
            version=agave_version,
            flags={
                "ledger_path": "/home/sol/ledger",
                "identity_pubkey": self.bootstrap_node.validator_key.public_key,
                "vote_pubkey": self.bootstrap_node.vote_account_key.public_key,
                "stake_pubkey": self.stake_account_key.public_key,
                "faucet_pubkey": self.faucet_key.public_key
            },
            primordial=[
                {
                    "pubkey": self.bootstrap_node.validator_key.public_key,
                    "lamports": "10000000000",  # 100 SOL
                },
                {
                    "pubkey": self.treasury_key.public_key,
                    "lamports": "100000000000000",  # 100000 SOL
                },
                {
                    "pubkey": self.faucet_key.public_key,
                    "lamports": "1000000000000",  # 1000 SOL
                },
            ],
            opts=pulumi.ResourceOptions(
                depends_on=[self.bootstrap_node.instance])
        )
