require("@nomicfoundation/hardhat-toolbox");
require("dotenv").config();

const GIWA_PRIVATE_KEY = process.env.GIWA_PRIVATE_KEY || "";

/** @type import('hardhat/config').HardhatUserConfig */
module.exports = {
  solidity: {
    version: "0.8.20",
    settings: {
      optimizer: { enabled: true, runs: 200 },
    },
  },
  networks: {
    // GIWA Sepolia — OP-Stack EVM L2 testnet
    giwaSepolia: {
      url: "https://sepolia-rpc.giwa.io/",
      chainId: 91342,
      accounts: GIWA_PRIVATE_KEY ? [GIWA_PRIVATE_KEY] : [],
      // No hardcoded gasPrice/gas — GIWA is a normal EIP-1559 OP-Stack chain,
      // let ethers/hardhat estimate.
    },
  },
  paths: {
    sources: "./src",
    artifacts: "./artifacts",
    cache: "./cache",
    tests: "./tests",
  },
};
