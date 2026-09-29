import * as path from 'path';
import * as cdk from 'aws-cdk-lib/core';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';

export interface CollectorStackProps extends cdk.StackProps {
  /** How often to snapshot station_status, in minutes. */
  snapshotMinutes: number;
}

/**
 * Snapshots the Citi Bike live feed into S3 on a schedule.
 *
 *   EventBridge rule (every N min) ──> Lambda ──> s3://<bucket>/raw/station_status/dt=.../*.json.gz
 *   EventBridge rule (daily)       ──> Lambda ──> s3://<bucket>/raw/station_information/dt=.../*.json.gz
 */
export class CollectorStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: CollectorStackProps) {
    super(scope, id, props);

    // The data is the one thing we can't recreate, so the bucket survives `cdk destroy`.
    const bucket = new s3.Bucket(this, 'Data', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    const snapshot = new lambda.Function(this, 'Snapshot', {
      runtime: lambda.Runtime.PYTHON_3_13,
      architecture: lambda.Architecture.ARM_64, // ~20% cheaper than x86 for the same work
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../collector/snapshot')),
      memorySize: 256,
      timeout: cdk.Duration.seconds(30),
      environment: { BUCKET: bucket.bucketName },
      logGroup: new logs.LogGroup(this, 'SnapshotLogs', {
        retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    });
    bucket.grantPut(snapshot); // write-only: the function can't read or delete data

    new events.Rule(this, 'StationStatusSchedule', {
      description: `Snapshot Citi Bike station_status every ${props.snapshotMinutes} min`,
      schedule: events.Schedule.rate(cdk.Duration.minutes(props.snapshotMinutes)),
      targets: [new targets.LambdaFunction(snapshot, {
        event: events.RuleTargetInput.fromObject({ feed: 'station_status' }),
        retryAttempts: 2,
      })],
    });

    new events.Rule(this, 'StationInformationSchedule', {
      description: 'Snapshot Citi Bike station_information daily (locations, capacity)',
      schedule: events.Schedule.cron({ minute: '0', hour: '8' }), // 08:00 UTC = 4am New York
      targets: [new targets.LambdaFunction(snapshot, {
        event: events.RuleTargetInput.fromObject({ feed: 'station_information' }),
        retryAttempts: 2,
      })],
    });

    new cdk.CfnOutput(this, 'BucketName', { value: bucket.bucketName });
    new cdk.CfnOutput(this, 'FunctionName', { value: snapshot.functionName });
  }
}
