/**
 * Deploy TojiboxOracle to GIWA Sepolia.
 *
 * Usage:
 *   npx hardhat run scripts/deploy.js --network giwaSepolia
 *
 * Required env vars:
 *   GIWA_PRIVATE_KEY     — deployer account private key (hex, no 0x prefix)
 *   ORACLE_ADDRESS       — EOA that the pipeline/CRE workflow will use to call commitBatch()
 *                          (can be same as deployer for testnet)
 *
 * After deploy, copy CONTRACT_ADDRESS into ../.env as TOJIBOX_ORACLE_ADDRESS
 */

const hre = require("hardhat");

async function main() {
  const [deployer] = await hre.ethers.getSigners();

  console.log("Deploying TojiboxOracle...");
  console.log("  Network  :", hre.network.name);
  console.log("  Deployer :", deployer.address);

  const balance = await hre.ethers.provider.getBalance(deployer.address);
  console.log("  Balance  :", hre.ethers.formatEther(balance), "ETH");

  // Oracle address — who is allowed to call commitBatch()
  // For testnet: use same deployer address
  // For mainnet: use the pipeline/CRE workflow EOA
  const oracleAddress = process.env.ORACLE_ADDRESS || deployer.address;
  console.log("  Oracle   :", oracleAddress);

  const TojiboxOracle = await hre.ethers.getContractFactory("TojiboxOracle");

  const contract = await TojiboxOracle.deploy(oracleAddress);
  await contract.waitForDeployment();

  const address = await contract.getAddress();

  console.log("\n✅ TojiboxOracle deployed!");
  console.log("  Contract address :", address);
  console.log("  Oracle address   :", oracleAddress);
  console.log("\nAdd to ../.env:");
  console.log(`  TOJIBOX_ORACLE_ADDRESS=${address}`);
  console.log(`  GIWA_NETWORK=${hre.network.name}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
