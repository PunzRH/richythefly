// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Script, console} from "forge-std/Script.sol";
import {IPoolManager} from "v4-core/src/interfaces/IPoolManager.sol";
import {RichyToken} from "../src/RichyToken.sol";
import {V4Launch} from "../src/V4Launch.sol";
import {RichyTreasury, ILaunchLP} from "../src/RichyTreasury.sol";

/// Richy's coin, dev'd by the fly's own wallet. ETH-quoted locked LP, 3% pool fee (buy + sell),
/// fees: FLY_BPS to the fly's trading wallet, rest to OTHER, coin-side fees burned.
/// NAME="Richy The Fly" SYMBOL=RICHY START_TICK=<coin-per-ETH tick> OTHER=0x... TOKEN_URI=https://... \
///   forge script script/LaunchRichy.s.sol --rpc-url $RH_RPC_URL --private-key $HOODFLY_PK --broadcast
contract LaunchRichy is Script {
    IPoolManager constant PM = IPoolManager(0x8366a39CC670B4001A1121B8F6A443A643e40951);

    struct Cfg {
        string name; string symbol; uint256 supply; int24 startTick; int24 span; uint24 fee; uint16 flyBps;
        address payable other; string uri; string website; string twitter;
    }

    function _cfg() internal view returns (Cfg memory c) {
        c.name = vm.envString("NAME");
        c.symbol = vm.envString("SYMBOL");
        c.supply = vm.envOr("SUPPLY", uint256(1_000_000_000e18));
        c.startTick = int24(vm.envInt("START_TICK"));
        c.span = int24(vm.envOr("SPAN", int256(69_000)));
        c.fee = uint24(vm.envOr("FEE", uint256(30_000)));
        c.flyBps = uint16(vm.envOr("FLY_BPS", uint256(8_000)));
        c.other = payable(vm.envAddress("OTHER"));
        c.uri = vm.envOr("TOKEN_URI", string(""));
        c.website = vm.envOr("WEBSITE", string("https://richythefly.com"));
        c.twitter = vm.envOr("TWITTER", string("https://x.com/RichyTheFly"));
    }

    function _token(Cfg memory c, address payable fly) internal returns (RichyToken) {
        return new RichyToken(c.name, c.symbol, c.supply, fly, c.uri, c.website, c.twitter);
    }

    function run() external {
        Cfg memory c = _cfg();
        vm.startBroadcast();
        address payable fly = payable(vm.envAddress("FLY")); // Richy's TRADING wallet: gets 80% of fees, never the dev bag
        address payable dev = payable(msg.sender);          // Richy's DEV wallet: deploys, holds the dev buy
        RichyTreasury treasury = new RichyTreasury(fly, c.other, c.flyBps);
        RichyToken coin = _token(c, dev);
        V4Launch launch = new V4Launch(PM, address(coin), address(0), c.fee, 200, c.startTick, c.span, address(treasury));
        coin.transfer(address(launch), c.supply);
        treasury.init(ILaunchLP(address(launch)));
        vm.stopBroadcast();
        console.log("coin:", address(coin));
        console.log("treasury:", address(treasury));
        console.log("launcher (locked LP):", address(launch));
        console.log("liquidity:", launch.liquidity());
        console.log("dev:", dev);
        console.log("fly (fee recipient):", fly);
        console.log("tickLower:", int256(launch.tickLower()));
        console.log("tickUpper:", int256(launch.tickUpper()));
    }
}
