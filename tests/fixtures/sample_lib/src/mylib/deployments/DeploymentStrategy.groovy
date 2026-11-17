package mylib.deployments

import mylib.config.TestScenario

/**
 * DeploymentStrategy interface defines the contract for deployment strategies.
 */
interface DeploymentStrategy {

    Boolean shouldWrapInstallWithStage = true
    Boolean shouldRunPreDeploymentChecks = false
    Boolean shouldRunSetupTasks = false
    Boolean shouldApplyEntitlements = true

    void preDeploymentChecks(Object context)

    void setupTasks(Object context)

    Map provision(Object context)

    Map install(Object context, Map provisionInfo)

    Map upgrade(Object context, Map provisionInfo)

    void cleanup(Object context, Map provisionInfo)

    void preCleanup(Object context, Map provisionInfo)

    Object getUpgradeScenario(TestScenario testScenario)

    Object getBackupRestoreScenario()

    Object getFreshInstallScenario()

}
