# Devnet-only throwaway keys

`program.json` and `deployer.json` exist only so GitHub Actions can deploy the escrow to **devnet** without any setup.
After deploying, the workflow removes the upgrade authority (`--final`), so these keys cannot change the program or touch any stakes.
Never use them on mainnet. A mainnet deploy uses fresh keys via `bash deploy` with `CALLIT_CLUSTER=mainnet-beta`.
