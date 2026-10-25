/* rnsmap-zoomfix.js — inlined into rns-map's index.html after textsize.js.
 * rns-map places its tooltip with left/top = mouse position. Under CSS zoom
 * on <html> those values are scaled again, so the tooltip lands at 1.5x the
 * mouse position at 150 %. Divide by the zoom after rns-map's own moveTip.
 */
(function () {
    'use strict';
    if (typeof moveTip !== 'function' || typeof tipEl === 'undefined') return;
    var orig = moveTip;
    window.moveTip = function (x, y) {
        orig(x, y);
        var z = parseFloat(document.documentElement.style.zoom) || 1;
        if (z !== 1) {
            tipEl.style.left = (parseFloat(tipEl.style.left) / z) + 'px';
            tipEl.style.top  = (parseFloat(tipEl.style.top)  / z) + 'px';
        }
    };
})();
