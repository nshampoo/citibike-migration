import * as cdk from 'aws-cdk-lib/core';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { CollectorStack } from '../lib/collector-stack';

const template = Template.fromStack(
  new CollectorStack(new cdk.App(), 'Test', { snapshotMinutes: 5 }),
);

test('station_status is snapshotted on clock-aligned minutes', () => {
  template.hasResourceProperties('AWS::Events::Rule', {
    ScheduleExpression: 'cron(0/5 * * * ? *)',
    Targets: [Match.objectLike({ Input: '{"feed":"station_status"}' })],
  });
});

test('the data bucket is kept if the stack is deleted', () => {
  template.hasResource('AWS::S3::Bucket', { DeletionPolicy: 'Retain', UpdateReplacePolicy: 'Retain' });
});

test('the function can only write to the bucket', () => {
  template.hasResourceProperties('AWS::IAM::Policy', {
    PolicyDocument: { Statement: [Match.objectLike({ Action: ['s3:PutObject', 's3:PutObjectLegalHold', 's3:PutObjectRetention', 's3:PutObjectTagging', 's3:PutObjectVersionTagging', 's3:Abort*'] })] },
  });
});

test('intervals that do not divide the hour are rejected', () => {
  expect(() => new CollectorStack(new cdk.App(), 'Bad', { snapshotMinutes: 7 })).toThrow(/divide 60/);
});
