"use strict";

const { enqueue } = require("./queue");

function processJob(job) {
  if (job.retry) {
    enqueue({ ...job, retry: false });
  }
  return { id: job.id, status: "done" };
}

module.exports = { processJob };
