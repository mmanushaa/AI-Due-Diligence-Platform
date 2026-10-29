"use strict";

const { processJob } = require("./worker");

const pending = [];

function enqueue(job) {
  pending.push(job);
  return processJob(job);
}

module.exports = { enqueue, pending };
