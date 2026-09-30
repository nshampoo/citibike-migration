#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib/core';
import { CollectorStack } from '../lib/collector-stack';

const app = new cdk.App();

// 1 min matches the feed's own refresh rate; coarser views are a filter later, finer never.
// Try another value without editing code: `cdk diff -c snapshotMinutes=5` (applies to that one command only).
const snapshotMinutes = Number(app.node.tryGetContext('snapshotMinutes') ?? 1);

new CollectorStack(app, 'CitibikeCollector', {
  env: { account: '404933715334', region: 'us-east-1' },
  snapshotMinutes,
  tags: { project: 'citibike-migration' },
});
