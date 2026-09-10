// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {ERC20} from "openzeppelin-contracts/contracts/token/ERC20/ERC20.sol";

/// @title RICHY: plain ERC20 (no tax, no owner, no mint) carrying its metadata link on chain.
/// @notice contractURI()/tokenURI() (EIP-7572 style) point at a JSON with image, website and X,
///         plus direct website()/twitter() getters so explorers and bots can show the links.
contract RichyToken is ERC20 {
    string private _uri;
    string public website;
    string public twitter;

    event ContractURIUpdated();

    constructor(string memory name_, string memory symbol_, uint256 supply, address recipient, string memory uri_, string memory website_, string memory twitter_)
        ERC20(name_, symbol_)
    {
        _uri = uri_;
        website = website_;
        twitter = twitter_;
        _mint(recipient, supply);
        emit ContractURIUpdated();
    }

    function contractURI() external view returns (string memory) { return _uri; }
    function tokenURI() external view returns (string memory) { return _uri; }
}
