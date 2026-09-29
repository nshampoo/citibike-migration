#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib/core';
import { CollectorStack } from '../lib/collector-stack';

const app = new cdk.App();

// Override without editing code: `cdk diff -c snapshotMinutes=2`
const snapshotMinutes = Number(app.node.tryGetContext('snapshotMinutes') ?? 5);

new CollectorStack(app, 'CitibikeCollector', {
  env: { account: '404933715334', region: 'us-east-1' },
  snapshotMinutes,
  tags: { project: 'citibike-migration' },
});
