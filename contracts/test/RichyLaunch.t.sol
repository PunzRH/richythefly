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

/// Fork test on Robinhood Chain: the fly's wallet launches RICHY (ETH-quoted, 3% fee, locked LP),
/// a buyer buys and sells, harvest() splits ETH fees 80/20 and burns coin fees.
contract RichyLaunchForkTest is Test {
    IPoolManager constant PM = IPoolManager(0x8366a39CC670B4001A1121B8F6A443A643e40951);
    uint256 constant SUPPLY = 1_000_000_000e18;
    int24 constant START_TICK = 200_000; // ~4.85e8 coin per ETH => start FDV ~2.06 ETH
    int24 constant SPAN = 69_000;
    address payable flyW = payable(address(0xF17));
    address payable other = payable(address(0x0741E5));
    RichyToken coin; V4Launch launch; RichyTreasury treasury; PoolSwapTest router;

    function setUp() public {
        vm.createSelectFork(vm.envString("RH_RPC_URL"));
        vm.deal(flyW, 1 ether);
        vm.startPrank(flyW);
        treasury = new RichyTreasury(flyW, other, 8000);
        coin = new RichyToken("Richy The Fly", "RICHY", SUPPLY, flyW, "https://gateway.pinata.cloud/ipfs/QmQgcTDRnhGARVgweYfC5kFerp2NFSjn9vkgV31DtDrtz4", "https://richythefly.com", "https://x.com/RichyTheFly");
        launch = new V4Launch(PM, address(coin), address(0), 30_000, 200, START_TICK, SPAN, address(treasury));
        coin.transfer(address(launch), SUPPLY);
        treasury.init(ILaunchLP(address(launch)));
        vm.stopPrank();
        router = new PoolSwapTest(PM);
    }

    function _key() internal view returns (PoolKey memory k) {
        (k.currency0, k.currency1, k.fee, k.tickSpacing, k.hooks) = launch.key();
    }

    function test_launch_trade_harvest_split() public {
        assertTrue(launch.launched());
        assertEq(treasury.deployer(), flyW);
        assertEq(coin.website(), "https://richythefly.com");
        assertEq(coin.twitter(), "https://x.com/RichyTheFly");
        address buyer = address(0xB0B);
        vm.deal(buyer, 2 ether);
        bool zeroForOne = !launch.coinIsCurrency0(); // ETH (currency0) -> coin
        vm.startPrank(buyer);
        router.swap{value: 0.5 ether}(
            _key(),
            SwapParams({zeroForOne: zeroForOne, amountSpecified: -0.5 ether, sqrtPriceLimitX96: zeroForOne ? TickMath.MIN_SQRT_PRICE + 1 : TickMath.MAX_SQRT_PRICE - 1}),
            PoolSwapTest.TestSettings({takeClaims: false, settleUsingBurn: false}), ""
        );
        uint256 got = coin.balanceOf(buyer);
        console.log("0.5 ETH bought coin:", got / 1e18);
        assertGt(got, 0);
        coin.approve(address(router), type(uint256).max);
        router.swap(
            _key(),
            SwapParams({zeroForOne: !zeroForOne, amountSpecified: -int256(got / 2), sqrtPriceLimitX96: !zeroForOne ? TickMath.MIN_SQRT_PRICE + 1 : TickMath.MAX_SQRT_PRICE - 1}),
            PoolSwapTest.TestSettings({takeClaims: false, settleUsingBurn: false}), ""
        );
        vm.stopPrank();
        uint256 flyBefore = flyW.balance; uint256 otherBefore = other.balance; uint256 dead = coin.balanceOf(address(0xdEaD));
        vm.prank(address(0xCA11));
        treasury.harvest();
        uint256 flyGot = flyW.balance - flyBefore; uint256 otherGot = other.balance - otherBefore;
        console.log("fly got wei:", flyGot); console.log("other got wei:", otherGot);
        console.log("coin burned:", (coin.balanceOf(address(0xdEaD)) - dead) / 1e18);
        // 3% of 0.5 ETH = 0.015 ETH in fees; 80% => 0.012, 20% => 0.003 (allow LP-share rounding)
        assertApproxEqRel(flyGot, 0.012 ether, 0.02e18);
        assertApproxEqRel(otherGot, 0.003 ether, 0.02e18);
        assertGt(coin.balanceOf(address(0xdEaD)) - dead, 0, "sell-side fees burned");
        assertEq(treasury.totalFlyEth(), flyGot);
        assertEq(address(treasury).balance, 0);
    }

    function test_init_only_deployer_once() public {
        vm.prank(address(0xBAD));
        vm.expectRevert(RichyTreasury.NotDeployer.selector);
        treasury.init(ILaunchLP(address(launch)));
        vm.prank(flyW);
        vm.expectRevert(RichyTreasury.AlreadyInit.selector);
        treasury.init(ILaunchLP(address(launch)));
    }
}
