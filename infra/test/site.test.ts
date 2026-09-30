import * as cdk from 'aws-cdk-lib/core';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { CollectorStack } from '../lib/collector-stack';
import { SiteStack } from '../lib/site-stack';

const app = new cdk.App();
const collector = new CollectorStack(app, 'Collector', { snapshotMinutes: 1 });
const template = Template.fromStack(new SiteStack(app, 'Site', { rawBucket: collector.bucket }));

test('deploying the page never deletes the builder\'s live data', () => {
  template.hasResourceProperties('Custom::CDKBucketDeployment', { Prune: false });
});

test('the site bucket is private and served only through CloudFront over HTTPS', () => {
  template.hasResourceProperties('AWS::S3::Bucket', {
    PublicAccessBlockConfiguration: { BlockPublicAcls: true, BlockPublicPolicy: true, IgnorePublicAcls: true, RestrictPublicBuckets: true },
  });
  template.hasResourceProperties('AWS::CloudFront::Distribution', {
    DistributionConfig: Match.objectLike({
      DefaultRootObject: 'index.html',
      DefaultCacheBehavior: Match.objectLike({ ViewerProtocolPolicy: 'redirect-to-https', Compress: true }),
    }),
  });
});

test('the builder runs every 5 minutes, just after the snapshot lands', () => {
  template.hasResourceProperties('AWS::Events::Rule', { ScheduleExpression: 'cron(2/5 * * * ? *)' });
});

test('the builder can only read the raw bucket', () => {
  const policies = Object.values(template.findResources('AWS::IAM::Policy'));
  const actions = policies.flatMap((p: any) => p.Properties.PolicyDocument.Statement)
    .filter((st: any) => JSON.stringify(st.Resource).includes('Collector'))
    .flatMap((st: any) => [].concat(st.Action));
  expect(actions.length).toBeGreaterThan(0);
  expect(actions.every((a: string) => a.startsWith('s3:Get') || a.startsWith('s3:List'))).toBe(true);
});
