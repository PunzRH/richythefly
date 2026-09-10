// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {IERC20} from "openzeppelin-contracts/contracts/token/ERC20/IERC20.sol";

interface ILaunchLP {
    function launch() external;
    function collectFees() external;
    function coin() external view returns (address);
}

/// @title RichyTreasury: owns Richy's locked LP and splits its fees.
/// @notice The V4Launch position (coin/ETH, 3% pool fee) is owned by this contract, so every
///         harvest lands here. ETH fees (3% of buys) are split FLY_BPS to the fly's trading
///         wallet and the rest to the second wallet. Coin fees (3% of sells) are burned, so the
///         dev never sells his own coin. Anyone may call harvest(); there is no owner after init.
contract RichyTreasury {
    address payable public immutable fly;
    address payable public immutable other;
    uint16 public immutable flyBps;
    address public immutable deployer;
    ILaunchLP public launchLP;
    IERC20 public coin;
    address constant DEAD = 0x000000000000000000000000000000000000dEaD;

    uint256 public totalFlyEth;
    uint256 public totalOtherEth;
    uint256 public totalCoinBurned;

    event Harvested(uint256 flyEth, uint256 otherEth, uint256 coinBurned);
    event Initialized(address launchLP, address coin);

    error NotDeployer();
    error AlreadyInit();

    constructor(address payable fly_, address payable other_, uint16 flyBps_) {
        require(fly_ != address(0) && other_ != address(0) && flyBps_ <= 10_000, "args");
        fly = fly_;
        other = other_;
        flyBps = flyBps_;
        deployer = msg.sender;
    }

    /// @notice One-shot: bind the launcher (whose owner is this contract) and open the pool.
    function init(ILaunchLP lp) external {
        if (msg.sender != deployer) revert NotDeployer();
        if (address(launchLP) != address(0)) revert AlreadyInit();
        launchLP = lp;
        coin = IERC20(lp.coin());
        lp.launch();
        emit Initialized(address(lp), address(coin));
    }

    /// @notice Pull accrued LP fees and split them. Permissionless.
    function harvest() external {
        launchLP.collectFees();
        uint256 eth = address(this).balance;
        uint256 toFly = eth * flyBps / 10_000;
        uint256 toOther = eth - toFly;
        if (toFly > 0) {
            (bool ok,) = fly.call{value: toFly}("");
            require(ok, "fly send");
        }
        if (toOther > 0) {
            (bool ok2,) = other.call{value: toOther}("");
            require(ok2, "other send");
        }
        uint256 c = coin.balanceOf(address(this));
        if (c > 0) coin.transfer(DEAD, c);
        totalFlyEth += toFly;
        totalOtherEth += toOther;
        totalCoinBurned += c;
        emit Harvested(toFly, toOther, c);
    }

    receive() external payable {}
}
