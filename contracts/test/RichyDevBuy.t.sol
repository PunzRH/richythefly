// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;
import {Test, console} from "forge-std/Test.sol";
import {IPoolManager} from "v4-core/src/interfaces/IPoolManager.sol";
import {PoolSwapTest} from "v4-core/src/test/PoolSwapTest.sol";
import {SwapParams} from "v4-core/src/types/PoolOperation.sol";
import {PoolKey} from "v4-core/src/types/PoolKey.sol";
import {TickMath} from "v4-core/src/libraries/TickMath.sol";
import {RichyToken} from "../src/RichyToken.sol";
import {V4Launch} from "../src/V4Launch.sol";
import {RichyTreasury, ILaunchLP} from "../src/RichyTreasury.sol";
contract RichyDevBuy is Test {
    IPoolManager constant PM = IPoolManager(0x8366a39CC670B4001A1121B8F6A443A643e40951);
    function test_sizes() public {
        vm.createSelectFork(vm.envString("RH_RPC_URL"));
        int24 startTick = int24(vm.envInt("START_TICK"));
        address payable fly = payable(address(0xF17)); vm.deal(fly, 10 ether);
        vm.startPrank(fly);
        RichyTreasury t = new RichyTreasury(fly, payable(address(0x1)), 8000);
        RichyToken coin = new RichyToken("Richy The Fly", "RICHY", 1_000_000_000e18, fly, "", "", "");
        V4Launch launch = new V4Launch(PM, address(coin), address(0), 30_000, 200, startTick, 69_000, address(t));
        coin.transfer(address(launch), 1_000_000_000e18);
        t.init(ILaunchLP(address(launch)));
        vm.stopPrank();
        PoolSwapTest router = new PoolSwapTest(PM);
        PoolKey memory k; (k.currency0, k.currency1, k.fee, k.tickSpacing, k.hooks) = launch.key();
        uint256[5] memory sizes = [uint256(0.105 ether), 0.11 ether, 0.115 ether, 0.12 ether, 0.125 ether];
        for (uint256 i = 0; i < sizes.length; i++) {
            uint256 snap = vm.snapshot();
            address b = address(uint160(0xB0B + i)); vm.deal(b, 10 ether);
            vm.prank(b);
            router.swap{value: sizes[i]}(k, SwapParams({zeroForOne: true, amountSpecified: -int256(sizes[i]), sqrtPriceLimitX96: TickMath.MIN_SQRT_PRICE + 1}), PoolSwapTest.TestSettings({takeClaims: false, settleUsingBurn: false}), "");
            console.log("buy wei:", sizes[i]); console.log("  tokens:", coin.balanceOf(b) / 1e18); console.log("  supply bps:", coin.balanceOf(b) * 10000 / 1_000_000_000e18);
            vm.revertTo(snap);
        }
    }
}
