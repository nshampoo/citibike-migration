#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib/core';
import { CollectorStack } from '../lib/collector-stack';
import { SiteStack } from '../lib/site-stack';

const app = new cdk.App();

// 1 min matches the feed's own refresh rate; coarser views are a filter later, finer never.
// Try another value without editing code: `cdk diff -c snapshotMinutes=5` (applies to that one command only).
const snapshotMinutes = Number(app.node.tryGetContext('snapshotMinutes') ?? 1);

const env = { account: '404933715334', region: 'us-east-1' };
const tags = { project: 'citibike-migration' };

const collector = new CollectorStack(app, 'CitibikeCollector', { env, snapshotMinutes, tags });

// Separate stack: the site can be torn down and rebuilt without touching the raw data.
new SiteStack(app, 'CitibikeSite', { env, tags, rawBucket: collector.bucket });
