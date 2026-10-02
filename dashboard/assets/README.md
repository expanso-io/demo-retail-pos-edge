# Storefront artwork

Cash register and eight shopper characters created and exported as SVG
in [Paper Design](https://app.paper.design/file/01M3ZAXF1FBD3SEHBR6QA3DD9S/p-1-0).
The shopper family follows the walking-character reference supplied by the user.
All artwork uses vector paths; the board loads it from this directory.

Each shopper has an independent duration, offset, and direction. Walking
indicates that at least one till is open, not a measured customer count.
Reduced-motion settings stop the walking animation. The header also has
a pause control.

The intake cylinder shows cumulative events accepted by the Expanso input
(sum of register `sent` counters). It does not claim database persistence.
The store button opens or closes its existing tills through the register
control API; it does not control Expanso jobs.
