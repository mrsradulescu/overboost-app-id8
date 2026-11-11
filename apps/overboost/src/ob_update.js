'use strict';
/* fără actualizator în mașină: actualizările se instalează cu mgu-deploy (aceeași cale ca aplicațiile IterioDev) */
const VERSION = "1.7.1";
exports.create = () => ({
    base: { version: VERSION, base: 3 }, st: { found: null, msg: '', busy: false },
    cur: () => null, check: () => Promise.resolve(), install: () => Promise.resolve(), remove: () => {},
});
